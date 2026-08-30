from datetime import datetime, timezone

import pytest
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.catalog import detect_episode_number
from app.database import Base, make_engine
from app.hierarchy_review import assign_known_videos_to_title, delete_empty_local_title
from app.migrations import migrate_schema
from app.models import CatalogCollection, CatalogTitle, Video, VideoVariantGroup
from app.numbering import (
    recalculate_title_numbering,
    set_duplicate_group_primary,
    summarize_title_numbering,
    unresolved_duplicate_groups,
)
from app.scanner import scan_library
from app.video_variants import (
    assign_video_catalog_title,
    assign_video_variant_group,
    create_video_variant_group,
    update_video_variant_group,
)


PROBE_RESULT = {
    "duration": 60.0,
    "video_codec": "h264",
    "width": 1920,
    "height": 1080,
    "audio": [],
    "subtitles": [],
}


def _title(
    collection: CatalogCollection,
    name: str,
    *,
    path: str | None = None,
) -> CatalogTitle:
    return CatalogTitle(
        collection=collection,
        local_title=name,
        normalized_local_title=name.casefold(),
        relative_root_path=path or f"{collection.relative_root_path}/{name}",
        part_type="season",
        season_number=1,
        season_label="S1",
    )


def _video(
    collection: CatalogCollection,
    title: CatalogTitle,
    filename: str = "E01.mkv",
) -> Video:
    return Video(
        relative_path=f"{title.relative_root_path}/{filename}",
        root_folder="Anime",
        filename=filename,
        size=1,
        mtime_ns=1,
        catalog_collection=collection,
        catalog_title=title,
    )


def _stored_title_graph(session: Session):
    collection = CatalogCollection(
        local_title="Show",
        normalized_local_title="show",
        relative_root_path="Anime/Show",
    )
    first = _title(collection, "Season 1", path="Anime/Show/Season 1")
    second = _title(collection, "Alternative", path="Anime/Show/Alternative")
    video = _video(collection, first)
    session.add_all([collection, first, second, video])
    session.flush()
    return collection, first, second, video


def test_variant_schema_migration_is_idempotent_and_does_not_backfill(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'variant-migration.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection, title, _second, video = _stored_title_graph(session)
        session.commit()
        video_id = video.id
        title_id = title.id

    # Simulate the committed pre-variant schema while retaining existing rows.
    legacy_columns = [
        column["name"] for column in inspect(engine).get_columns("videos")
        if column["name"] != "video_variant_group_id"
    ]
    projected_columns = ", ".join(f'"{name}"' for name in legacy_columns)
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        create_sql = connection.scalar(text(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'videos'"
        ))
        legacy_sql = create_sql.replace(
            "CREATE TABLE videos (", "CREATE TABLE videos_legacy (", 1
        ).replace(
            "\tvideo_variant_group_id INTEGER, \n", ""
        ).replace(
            "\tFOREIGN KEY(video_variant_group_id) REFERENCES "
            "video_variant_groups (id) ON DELETE SET NULL, \n",
            "",
        )
        connection.execute(text("PRAGMA foreign_keys=OFF"))
        connection.exec_driver_sql(legacy_sql)
        connection.execute(text(
            f"INSERT INTO videos_legacy ({projected_columns}) "
            f"SELECT {projected_columns} FROM videos"
        ))
        connection.execute(text("DROP TABLE videos"))
        connection.execute(text("ALTER TABLE videos_legacy RENAME TO videos"))
        connection.execute(text("DROP TABLE video_variant_groups"))
        connection.execute(text("PRAGMA foreign_keys=ON"))

    migrate_schema(engine)
    migrate_schema(engine)

    schema = inspect(engine)
    assert schema.get_table_names().count("video_variant_groups") == 1
    assert [
        column["name"] for column in schema.get_columns("videos")
    ].count("video_variant_group_id") == 1
    video_fk = next(
        item for item in schema.get_foreign_keys("videos")
        if item["constrained_columns"] == ["video_variant_group_id"]
    )
    group_fk = next(
        item for item in schema.get_foreign_keys("video_variant_groups")
        if item["constrained_columns"] == ["catalog_title_id"]
    )
    assert (
        video_fk["referred_table"],
        video_fk["referred_columns"],
    ) == ("video_variant_groups", ["id"])
    with engine.connect() as connection:
        variant_fk_pragma = next(
            row for row in connection.execute(text("PRAGMA foreign_key_list(videos)"))
            if row.table == "video_variant_groups"
        )
    assert variant_fk_pragma.on_delete == "SET NULL"
    assert (
        group_fk["referred_table"],
        group_fk["referred_columns"],
        group_fk["options"].get("ondelete"),
    ) == ("catalog_titles", ["id"], "CASCADE")
    assert any(
        item["name"] == "ix_videos_video_variant_group_id"
        and item["column_names"] == ["video_variant_group_id"]
        for item in schema.get_indexes("videos")
    )
    assert any(
        item["name"] == "ix_video_variant_groups_catalog_title_id"
        and item["column_names"] == ["catalog_title_id"]
        for item in schema.get_indexes("video_variant_groups")
    )
    with Session(engine) as session:
        assert session.get(Video, video_id).video_variant_group_id is None
        assert session.get(Video, video_id).catalog_title_id == title_id
        assert session.scalar(
            select(func.count()).select_from(VideoVariantGroup)
        ) == 0
        assert list(session.execute(text("PRAGMA foreign_key_check"))) == []
        group = create_video_variant_group(
            session.get(CatalogTitle, title_id),
            manual_label="temporary",
        )
        session.add(group)
        session.flush()
        assign_video_variant_group(session.get(Video, video_id), group)
        session.commit()
        group_id = group.id

    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM video_variant_groups WHERE id = :group_id"),
            {"group_id": group_id},
        )
    with Session(engine) as session:
        assert session.get(Video, video_id).video_variant_group_id is None
        assert session.get(Video, video_id).catalog_title_id == title_id
        assert list(session.execute(text("PRAGMA foreign_key_check"))) == []


def test_create_and_update_group_preserves_stable_identity_and_nullable_axes(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'group-identity.db'}")
    Base.metadata.create_all(engine)
    verified_at = datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    with Session(engine) as session:
        _collection, title, _second, _video_item = _stored_title_graph(session)
        group = create_video_variant_group(
            title,
            manual_label="  A  ",
            verified_at=verified_at,
        )
        session.add(group)
        session.flush()
        group_id = group.id
        assert (
            group.catalog_title_id,
            group.manual_label,
            group.release_source,
            group.content_variant,
            group.note,
        ) == (title.id, "A", None, None, None)

        update_video_variant_group(
            group,
            manual_label="BD lane",
            release_source="BD",
            content_variant="Uncensored",
            note="  manually reviewed  ",
        )
        session.flush()
        assert group.id == group_id
        assert (
            group.manual_label,
            group.release_source,
            group.content_variant,
            group.note,
        ) == ("BD lane", "bd", "uncensored", "manually reviewed")

        with pytest.raises(ValueError, match="nesmí být prázdné"):
            update_video_variant_group(group, manual_label="  ")
        with pytest.raises(ValueError, match="release source"):
            update_video_variant_group(
                group, manual_label="BD lane", release_source="laserdisc"
            )
        assert group.id == group_id


def test_same_title_assignment_clear_and_cross_title_rejection_are_atomic(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'assignment.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        _collection, first, second, video = _stored_title_graph(session)
        first_group = create_video_variant_group(first, manual_label="TV")
        second_group = create_video_variant_group(second, manual_label="BD")
        session.add_all([first_group, second_group])
        session.flush()

        assign_video_variant_group(video, first_group)
        session.commit()
        video_id, first_group_id = video.id, first_group.id
        assert video.video_variant_group_id == first_group.id

        with pytest.raises(ValueError, match="stejného CatalogTitle"):
            assign_video_variant_group(video, second_group)
        assert video.catalog_title is first
        assert video.video_variant_group is first_group

        with pytest.raises(ValueError, match="stejného CatalogTitle"):
            assign_video_catalog_title(
                video,
                second,
                video_variant_group=first_group,
            )
        assert video.catalog_title is first
        assert video.video_variant_group is first_group

        session.rollback()
        video = session.get(Video, video_id)
        first_group = session.get(VideoVariantGroup, first_group_id)
        assert video.catalog_title_id == first.id
        assert video.video_variant_group_id == first_group.id

        with pytest.raises(ValueError, match="stejného CatalogTitle"):
            assign_video_catalog_title(
                video,
                None,
                video_variant_group=first_group,
            )
        assert video.catalog_title is first
        assert video.video_variant_group is first_group

        assign_video_variant_group(video, None)
        session.flush()
        assert video.video_variant_group_id is None


def test_manual_title_move_clears_old_group_without_creating_target_group(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'move.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection, first, second, video = _stored_title_graph(session)
        group = create_video_variant_group(first, manual_label="TV")
        session.add(group)
        session.flush()
        assign_video_variant_group(video, group)
        session.commit()
        collection_id, video_id, second_id, group_id = (
            collection.id, video.id, second.id, group.id,
        )

    with Session(engine) as session:
        assign_known_videos_to_title(session, [video_id], second_id)
        session.commit()

    with Session(engine) as session:
        moved = session.get(Video, video_id)
        assert moved.catalog_title_id == second_id
        assert moved.video_variant_group_id is None
        assert session.get(VideoVariantGroup, group_id) is not None
        assert session.scalar(
            select(func.count()).select_from(VideoVariantGroup)
        ) == 1
        assert session.get(CatalogCollection, collection_id) is not None


def test_group_assignment_survives_rescan_and_startup(tmp_path, monkeypatch):
    media = tmp_path / "Show" / "E01.mkv"
    media.parent.mkdir()
    media.write_bytes(b"video")
    monkeypatch.setattr("app.scanner.service.probe_video", lambda _, **__: PROBE_RESULT)
    engine = make_engine(f"sqlite:///{tmp_path / 'lifecycle.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)

    with sessions() as session:
        scan_library(session, tmp_path)
        video = session.scalar(select(Video))
        group = create_video_variant_group(
            video.catalog_title,
            manual_label="TV",
            release_source="tv",
        )
        session.add(group)
        session.flush()
        assign_video_variant_group(video, group)
        session.commit()
        video_id, group_id, title_id = video.id, group.id, video.catalog_title_id
        expected_summary = summarize_title_numbering(
            [video], video.catalog_title
        )
        assert (
            expected_summary.physical_video_count,
            expected_summary.logical_episode_count,
            expected_summary.confirmed_variant_instance_count,
        ) == (1, 1, 1)

    with sessions() as session:
        scan_library(session, tmp_path)
        video = session.get(Video, video_id)
        assert (video.catalog_title_id, video.video_variant_group_id) == (
            title_id, group_id,
        )
        summary = summarize_title_numbering([video], video.catalog_title)
        assert summary == expected_summary

    migrate_schema(engine)
    with sessions() as session:
        video = session.get(Video, video_id)
        assert (video.catalog_title_id, video.video_variant_group_id) == (
            title_id, group_id,
        )
        assert session.scalar(
            select(func.count()).select_from(VideoVariantGroup)
        ) == 1
        summary = summarize_title_numbering([video], video.catalog_title)
        assert summary == expected_summary


def test_startup_preserves_empty_title_that_owns_manual_variant_group(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'empty-group-startup.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection = CatalogCollection(
            local_title="Manual",
            normalized_local_title="manual",
            relative_root_path="Anime/Manual",
        )
        title = _title(
            collection,
            "Archived lane",
            path="Anime/Manual/.catalog-part-1",
        )
        session.add_all([collection, title])
        session.flush()
        group = create_video_variant_group(title, manual_label="A")
        session.add(group)
        session.commit()
        title_id, group_id = title.id, group.id

    migrate_schema(engine)
    migrate_schema(engine)

    with Session(engine) as session:
        assert session.get(CatalogTitle, title_id) is not None
        assert session.get(VideoVariantGroup, group_id).catalog_title_id == title_id


def test_delete_video_keeps_group_and_delete_empty_title_cascades_group(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'delete.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection, title, empty_title, video = _stored_title_graph(session)
        retained = create_video_variant_group(title, manual_label="TV")
        cascading = create_video_variant_group(empty_title, manual_label="BD")
        session.add_all([retained, cascading])
        session.flush()
        assign_video_variant_group(video, retained)
        session.commit()
        video_id, retained_id = video.id, retained.id
        collection_id = collection.id
        empty_title_id, cascading_id = empty_title.id, cascading.id

    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM videos WHERE id = :video_id"),
            {"video_id": video_id},
        )
    with Session(engine) as session:
        assert session.get(VideoVariantGroup, retained_id) is not None

    with Session(engine) as session:
        delete_empty_local_title(session, collection_id, empty_title_id)
        session.commit()
    with Session(engine) as session:
        assert session.get(VideoVariantGroup, cascading_id) is None
        assert session.get(VideoVariantGroup, retained_id) is not None
        assert list(session.execute(text("PRAGMA foreign_key_check"))) == []


def test_database_constraints_reject_invalid_group_taxonomy(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'constraints.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        _collection, title, _second, _video_item = _stored_title_graph(session)
        session.commit()
        title_id = title.id

    with Session(engine) as session:
        session.add(VideoVariantGroup(
            catalog_title_id=title_id,
            manual_label="invalid",
            release_source="vhs",
        ))
        with pytest.raises(IntegrityError):
            session.commit()


def test_null_groups_leave_confirmed_duplicate_and_numbering_semantics_unchanged():
    collection = CatalogCollection(
        id=1,
        local_title="Show",
        normalized_local_title="show",
        relative_root_path="Anime/Show",
    )
    title = _title(collection, "Season 1")
    title.id = 10
    first = _video(collection, title, "Show - 01.mkv")
    copy = _video(collection, title, "Show - 01 copy.mkv")
    second = _video(collection, title, "Show - 02.mkv")
    first.id, copy.id, second.id = 1, 2, 3
    for video, number in ((first, 1), (copy, 1), (second, 2)):
        video.catalog_title_id = title.id
        video.catalog_collection_id = collection.id
        video.season_episode_number = number

    unresolved_before = unresolved_duplicate_groups([first, copy, second])
    summary_before = summarize_title_numbering([first, copy, second], title)
    assert len(unresolved_before) == 1

    set_duplicate_group_primary([first, copy], first)
    unresolved_after = unresolved_duplicate_groups([first, copy, second])
    summary_after = summarize_title_numbering([first, copy, second], title)

    assert all(video.video_variant_group_id is None for video in (first, copy, second))
    assert copy.duplicate_of is first
    assert unresolved_after == ()
    assert summary_before.physical_video_count == 3
    assert summary_before.logical_episode_count == summary_before.standard_total == 2
    assert summary_before.unassigned_variant_video_count == 3
    assert summary_before.duplicate_numbers == (1,)
    assert summary_after.standard_total == 2
    assert summary_after.confirmed_duplicates == 1


@pytest.mark.parametrize(("filename", "kind", "marker", "version_hint"), (
    ("Title - 01A.mkv", "structural_variant", "A", None),
    ("Title - 01B.mkv", "structural_variant", "B", None),
    ("Title - 02 Ver.TV.mkv", "standard", None, "Ver.TV"),
    ("Title - 03 (UC).mkv", "standard", None, "UC"),
))
def test_parser_evidence_does_not_become_group_authority(
    filename, kind, marker, version_hint,
):
    detection = detect_episode_number(filename)
    assert detection.kind == kind
    assert detection.structural_marker == marker
    assert detection.version_hint == version_hint
    video = Video(
        relative_path=f"Anime/Show/{filename}",
        root_folder="Anime",
        filename=filename,
        size=1,
        mtime_ns=1,
    )
    if kind == "structural_variant":
        collection = CatalogCollection(
            id=1,
            local_title="Show",
            normalized_local_title="show",
            relative_root_path="Anime/Show",
        )
        title = _title(collection, "Season 1")
        video.catalog_collection = collection
        video.catalog_title = title
        recalculate_title_numbering(title, [video])
        assert video.local_episode_number is None
        assert video.season_episode_number is None
        assert video.episode_number_source == "structural_variant"
    assert video.video_variant_group_id is None
    assert video.video_variant_group is None
