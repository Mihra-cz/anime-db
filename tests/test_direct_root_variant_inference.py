import pytest
from sqlalchemy import delete, event, select
from sqlalchemy.orm import Session, raiseload

from app.database import Base, make_engine
from app.hierarchy_authority import activate_manual_hierarchy_snapshot
from app.hierarchy_evaluation import evaluate_collection_hierarchy, finalize_collection_hierarchy
from app.hierarchy_rebuild import apply_hierarchy_rebuild_plan, build_hierarchy_rebuild_plan
from app.hierarchy_review import refresh_collection_state
from app.migrations import migrate_schema_at_startup
from app.models import CatalogCollection, Video, VideoVariantGroup, utc_now
from app.numbering import (
    recalculate_title_numbering, set_duplicate_group_primary,
    summarize_title_numbering, unresolved_duplicate_groups,
)
from app.scanner import scan_library
from app.structural_inference import direct_root_episode_profile
from app.supplementary import supplementary_inventory
from app.video_variants import (
    VariantGroupDraft, apply_video_variant_assignments, preview_video_variant_assignments,
)


def scan_series(tmp_path, monkeypatch, count=12):
    library = tmp_path / "library"
    root = library / "Show"
    root.mkdir(parents=True)
    for number in range(1, count + 1):
        for suffix in ("", " Ver.TV"):
            (root / f"Show - {number:02d}{suffix}.mkv").write_bytes(b"video")
    monkeypatch.setattr("app.scanner.service.probe_video", lambda *_args, **_kwargs: {
        "duration": 60.0, "video_codec": "h264", "width": 1920, "height": 1080,
        "audio": [], "subtitles": [],
    })
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        scan_library(session, library)
    return engine, library


def confirm_variants(session, count, *, same_group=False):
    collection = session.scalar(select(CatalogCollection))
    title, = collection.titles
    assignments = tuple(
        (video.id, "tv" if "Ver.TV" in video.filename and not same_group else "a")
        for video in title.videos if video.local_episode_number <= count
    )
    drafts = (VariantGroupDraft(key="a", manual_label="A"),)
    if not same_group:
        drafts += (VariantGroupDraft(key="tv", manual_label="TV", release_source="tv"),)
    preview = preview_video_variant_assignments(
        session, collection.id, title.id, assignments=assignments, drafts=drafts,
    )
    apply_video_variant_assignments(
        session, collection.id, title.id, assignments=assignments, drafts=drafts,
        expected_fingerprint=preview.fingerprint,
    )
    session.commit()


def snapshot(session):
    collection = session.scalar(select(CatalogCollection))
    title, = collection.titles
    videos = list(title.videos)
    profile = direct_root_episode_profile(videos)
    summary = summarize_title_numbering(videos, title)
    evaluation = evaluate_collection_hierarchy(collection, videos)
    return (
        profile, summary.physical_video_count, summary.logical_episode_count,
        title.effective_part_type, title.effective_season_number,
        collection.hierarchy_status, tuple(issue.code.value for issue in evaluation.blocking_issues),
        tuple((video.id, video.video_variant_group_id) for video in sorted(videos, key=lambda v: v.id)),
        (title.part_type_manual, title.season_number_manual, title.hierarchy_verified_at),
    )


@pytest.mark.parametrize("confirmed,same_group", [(0, False), (6, False), (12, False), (12, True)])
def test_public_variant_confirmation_only_resolves_authoritative_lanes(tmp_path, monkeypatch, confirmed, same_group):
    engine, _library = scan_series(tmp_path, monkeypatch)
    with Session(engine) as session:
        if confirmed:
            confirm_variants(session, confirmed, same_group=same_group)
        profile, physical, logical, kind, season, status, issues, *_ = snapshot(session)
        expected_unresolved = tuple(range(1 if same_group else confirmed + 1, 13))
        assert physical == 24
        assert profile.standard_count == logical == 12
        assert profile.unresolved_duplicate_numbers == expected_unresolved
        assert len(unresolved_duplicate_groups(list(session.scalars(select(Video))))) == len(expected_unresolved)
        if expected_unresolved:
            assert not profile.supports_automatic_season_one
            assert (kind, season, status) == ("title", None, "review_required")
            assert "generic_structural_type" in issues
        else:
            assert profile.supports_automatic_season_one
            assert (kind, season, status, issues) == ("season", 1, "automatic", ())
    engine.dispose()


@pytest.mark.parametrize("manual", [False, True])
def test_confirmed_direct_root_variants_survive_shared_lifecycle(tmp_path, monkeypatch, manual):
    engine, library = scan_series(tmp_path, monkeypatch)
    with Session(engine) as session:
        collection = session.scalar(select(CatalogCollection))
        title, = collection.titles
        if manual:
            activate_manual_hierarchy_snapshot(
                title, part_type="season", season_number=1, part_number=None,
                season_label="S1", sort_order=None, verified_at=utc_now(),
            )
            session.commit()
        confirm_variants(session, 12)
        expected = snapshot(session)
        assert expected[0].standard_count == expected[2] == 12
        assert expected[0].unresolved_duplicate_numbers == ()
        assert expected[3:7] == ("season", 1, "verified" if manual else "automatic", ())
        for finalize in (finalize_collection_hierarchy, refresh_collection_state):
            finalize(collection)
            session.commit()
            assert snapshot(session) == expected
        scan_library(session, library)
        assert snapshot(session) == expected

    assert migrate_schema_at_startup(engine) is True
    assert migrate_schema_at_startup(engine) is False
    with Session(engine) as session:
        assert snapshot(session) == expected
        # Exercise rebuild apply as well as the already-correct/no-op plan.
        collection = session.scalar(select(CatalogCollection))
        collection.hierarchy_status = "review_required"
        collection.hierarchy_note = "Stale automatic state"
        if not manual:
            title, = collection.titles
            title.part_type, title.season_number, title.season_label = "title", None, None
        session.commit()
        plan = build_hierarchy_rebuild_plan(session)
        assert plan.has_changes
        assert apply_hierarchy_rebuild_plan(session, plan).applied
        session.commit()
        assert snapshot(session) == expected
    engine.dispose()


@pytest.mark.parametrize("count,issue", [(15, None), (24, None), (25, "long_flat_series")])
def test_confirmed_variants_keep_logical_length_gates(tmp_path, monkeypatch, count, issue):
    engine, _library = scan_series(tmp_path, monkeypatch, count)
    with Session(engine) as session:
        confirm_variants(session, count)
        profile, physical, logical, kind, season, status, issues, *_ = snapshot(session)
        assert physical == 2 * count
        assert profile.standard_count == logical == count
        assert profile.unresolved_duplicate_numbers == ()
        assert (kind, season) == ("season", 1)
        assert issues == ((issue,) if issue else ())
        collection = session.scalar(select(CatalogCollection))
        soft = evaluate_collection_hierarchy(collection).soft_warnings
        assert tuple(i.code.value for i in soft) == (() if issue else ("soft_long_flat_series",))
    engine.dispose()


def test_missing_group_becomes_unknown_and_cannot_explain_repetition(tmp_path, monkeypatch):
    engine, _library = scan_series(tmp_path, monkeypatch)
    with Session(engine) as session:
        confirm_variants(session, 12)
        # Existing FK semantics retire a missing group to NULL, never a known lane.
        group_id = session.scalar(select(VideoVariantGroup.id).where(VideoVariantGroup.manual_label == "TV"))
        session.execute(delete(VideoVariantGroup).where(VideoVariantGroup.id == group_id))
        session.commit()
        session.expire_all()
        collection = session.scalar(select(CatalogCollection))
        assert sum(v.video_variant_group_id is None for v in collection.videos) == 12
        finalize_collection_hierarchy(collection)
        assert direct_root_episode_profile(list(collection.videos)).unresolved_duplicate_numbers == tuple(range(1, 13))
        assert collection.hierarchy_status == "review_required"
    engine.dispose()


def test_profile_uses_loaded_scalars_without_queries_or_writes(tmp_path, monkeypatch):
    engine, _library = scan_series(tmp_path, monkeypatch)
    with Session(engine) as session:
        confirm_variants(session, 12)
        first = session.scalar(select(Video).order_by(Video.id))
        copy = Video(
            relative_path="Show/copy.mkv", filename=first.filename, root_folder="Show",
            size=1, mtime_ns=1, catalog_title=first.catalog_title,
            catalog_collection=first.catalog_collection,
        )
        session.add(copy)
        session.flush()
        recalculate_title_numbering(first.catalog_title, list(first.catalog_title.videos))
        set_duplicate_group_primary([first, copy], first)
        session.commit()
    with Session(engine) as session:
        videos = list(session.scalars(select(Video).options(raiseload("*"))))
        # Inference must work before canonical projection without loading relations.
        for video in videos:
            video.local_episode_number = video.season_episode_number = None
        session.commit()
        videos = list(session.scalars(select(Video).options(raiseload("*"))))
        statements = []
        def record(_conn, _cursor, statement, *_args):
            statements.append(statement)
        event.listen(engine, "before_cursor_execute", record)
        try:
            before = [dict(video.__dict__) for video in videos]
            profile = direct_root_episode_profile(videos)
            assert profile.standard_count == 12
            assert profile.unresolved_duplicate_numbers == ()
            assert profile.supports_automatic_season_one
            assert [dict(video.__dict__) for video in videos] == before
            assert not session.dirty
            assert statements == []
        finally:
            event.remove(engine, "before_cursor_execute", record)
    engine.dispose()


def test_supplementary_media_parts_do_not_enter_standard_profile():
    from test_structural_inference import direct_root_collection

    collection, title = direct_root_collection(12)
    parts = [Video(
        id=12 + index, relative_path=f"Anime/Show/OVA01 Part {index}.mkv",
        filename=f"OVA01 Part {index}.mkv", root_folder="Anime", size=1, mtime_ns=1,
        media_part_number=index, catalog_title=title, catalog_collection=collection,
    ) for index in (1, 2)]
    assert supplementary_inventory(parts, title).logical_count == 1
    assert direct_root_episode_profile(list(title.videos)).standard_count == 12
    finalize_collection_hierarchy(collection)
    assert summarize_title_numbering(list(title.videos), title).standard_total == 12
    assert collection.hierarchy_status == "automatic"
