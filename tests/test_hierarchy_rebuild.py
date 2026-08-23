from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.hierarchy_rebuild import rebuild_hierarchy
from app.models import (
    CatalogCollection, CatalogTitle, ExternalTitleLink, InternalSubtitle, Video,
)


def seeded_engine():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        wrong_collections = []
        for index, roman in enumerate(("I", "II", "III", "IV"), 1):
            path = f"Anime/OVERLORD (L15-L22)/OVERLORD {roman}"
            collection = CatalogCollection(
                local_title=f"OVERLORD {roman}", normalized_local_title=f"overlord {roman.lower()}",
                relative_root_path=path,
            )
            title = CatalogTitle(
                local_title=f"OVERLORD {roman}", normalized_local_title=f"overlord {roman.lower()}",
                relative_root_path=path, collection=collection,
            )
            video = Video(
                relative_path=f"{path}/Episode 01.mkv", root_folder="Anime",
                filename="Episode 01.mkv", size=1, mtime_ns=1,
                catalog_title=title, manual_hardsub_cs=index == 1,
            )
            session.add_all([collection, title, video])
            wrong_collections.append(collection)
        session.flush()
        first_title = session.scalar(select(CatalogTitle).where(CatalogTitle.local_title == "OVERLORD I"))
        first_video = session.scalar(select(Video).where(Video.catalog_title_id == first_title.id))
        session.add(InternalSubtitle(
            video_id=first_video.id, stream_index=1, language="cze",
            normalized_language="cs",
        ))
        session.add(ExternalTitleLink(
            catalog_title_id=first_title.id, provider="anilist", external_id="1",
            match_method="manual_search", is_primary=True, is_manual=True,
        ))
        session.commit()
    return engine


def test_dry_run_reports_but_changes_nothing():
    engine = seeded_engine()
    with Session(engine) as session:
        changes = rebuild_hierarchy(session, apply=False)
    assert len(changes) == 4
    assert all(change.reason == "roman_sibling_same_base" for change in changes)
    with Session(engine) as session:
        assert all(title.season_number is None for title in session.scalars(select(CatalogTitle)))


def test_apply_repairs_only_safe_titles_and_is_idempotent():
    engine = seeded_engine()
    with Session(engine) as session:
        changes = rebuild_hierarchy(session, apply=True)
    assert len(changes) == 4
    with Session(engine) as session:
        titles = session.scalars(select(CatalogTitle).order_by(CatalogTitle.sort_order)).all()
        repaired = [title for title in titles if title.season_number is not None]
        assert [title.season_number for title in repaired] == [1, 2, 3, 4]
        assert len({title.catalog_collection_id for title in repaired}) == 1
        first = next(title for title in repaired if title.season_number == 1)
        assert session.scalar(select(ExternalTitleLink).where(
            ExternalTitleLink.catalog_title_id == first.id
        )).external_id == "1"
        video = session.scalar(select(Video).where(Video.catalog_title_id == first.id))
        assert video.manual_hardsub_cs is True
        assert session.scalar(select(InternalSubtitle).where(
            InternalSubtitle.video_id == video.id
        )).normalized_language == "cs"
        assert rebuild_hierarchy(session, apply=True) == []


def test_manual_hierarchy_override_is_not_changed():
    engine = seeded_engine()
    with Session(engine) as session:
        title = session.scalar(select(CatalogTitle).where(CatalogTitle.local_title == "OVERLORD II"))
        title.hierarchy_manual_override = True
        title.season_number, title.season_label = 9, "S9"
        session.commit()
        rebuild_hierarchy(session, apply=True)
        session.refresh(title)
        assert (title.season_number, title.season_label) == (9, "S9")


def test_manual_hierarchy_fields_have_priority_and_survive_rebuild():
    engine = seeded_engine()
    with Session(engine) as session:
        title = session.scalar(select(CatalogTitle).where(CatalogTitle.local_title == "OVERLORD II"))
        title.season_number_manual = 2
        title.part_number_manual = 1
        title.season_label_manual = "S2"
        title.part_type_manual = "part"
        title.sort_order_manual = 20
        title.hierarchy_manual_override = True
        session.commit()

        rebuild_hierarchy(session, apply=True)
        session.refresh(title)

        assert title.effective_season_number == 2
        assert title.effective_season_label == "S2"
        assert title.effective_part_type == "part"
        assert title.effective_part_number == 1
        assert title.effective_sort_order == 20
        assert title.season_number_manual == 2
        assert title.part_number_manual == 1


def test_rebuild_preserves_nested_parent_season_and_part_ordinal():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection = CatalogCollection(
            local_title="Wrong", normalized_local_title="wrong",
            relative_root_path="Anime/Show/Season 1/Part 2",
        )
        title = CatalogTitle(
            collection=collection, local_title="Wrong", normalized_local_title="wrong",
            relative_root_path="Anime/Show/Season 1/Part 2", part_type="title",
        )
        Video(
            relative_path="Anime/Show/Season 1/Part 2/E01.mkv",
            root_folder="Anime", filename="E01.mkv", size=1, mtime_ns=1,
            catalog_title=title, catalog_collection=collection,
        )
        session.add(collection)
        session.commit()

        changes = rebuild_hierarchy(session, apply=True)
        session.refresh(title)

        assert len(changes) == 1
        assert title.part_type == "part"
        assert title.season_number == 1
        assert title.part_number == 2
        assert title.season_label == "S1"


def test_rebuild_uses_shared_direct_root_season_one_inference():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection = CatalogCollection(
            local_title="Show", normalized_local_title="show",
            relative_root_path="Anime/Show",
        )
        title = CatalogTitle(
            collection=collection, local_title="Show", normalized_local_title="show",
            relative_root_path="Anime/Show", part_type="title",
        )
        for number in range(1, 13):
            Video(
                relative_path=f"Anime/Show/Show - {number:02}.mkv",
                root_folder="Anime", filename=f"Show - {number:02}.mkv",
                size=1, mtime_ns=number, catalog_title=title,
                catalog_collection=collection,
            )
        session.add(collection)
        session.commit()

        changes = rebuild_hierarchy(session, apply=True)
        session.refresh(title)

        assert len(changes) == 1
        assert changes[0].reason == "direct_root_contiguous_episode_sequence"
        assert (title.part_type, title.season_number, title.season_label) == (
            "season", 1, "S1",
        )
        assert title.part_type_manual is None
        assert title.hierarchy_manual_override is False
