import asyncio
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, object_session, selectinload, sessionmaker
from starlette.requests import Request

from app.config import Settings
from app.database import Base, make_engine, make_session_factory
from app.hierarchy_evaluation import finalize_hierarchy_write
from app.hierarchy_review import (
    confirm_existing_split_season_parts, parse_manual_definitions,
    parse_simple_definitions, set_manual_title_hierarchy, simple_definition_rows,
)
from app.main import create_app
from app.metadata.candidates import local_episode_count_evidence
from app.metadata.completion import resolve_metadata_completion
from app.metadata.service import unlink_title_metadata
from app.metadata.split import (
    MetadataSplitStatus, apply_metadata_split, evaluate_metadata_split,
)
from app.models import (
    CatalogCollection, CatalogTitle, ExternalTitleLink, TitleMetadata, Video,
    VideoVariantGroup, utc_now,
)
from app.numbering import (
    DuplicateRelationState, PART_LOCAL_NUMBERING_MODE, duplicate_relation_state,
    logical_episode_partitions, set_duplicate_group_primary, set_title_numbering,
    set_video_episode_override,
)
from app.page_edit_save import apply_title_numbering_edit
from app.part_local_numbering import (
    AMBIGUOUS_IDENTITY_REASON, EXISTING_AUTHORITY_REASON, HIERARCHY_ISSUE_REASON,
    MANUAL_CONFLICT_REASON, RECAP_REASON, apply_part_local_numbering,
    evaluate_part_local_numbering,
)
from app.scanner import scan_library
from app.supplementary import supplementary_ordinal

PROBE_RESULT = {
    "duration": 60.0, "video_codec": "h264", "width": 1920, "height": 1080,
    "audio": [], "subtitles": [],
}


# --- builders ------------------------------------------------------------------

def attach_metadata(title, count, external_id):
    title.metadata_status = "linked_manual"
    title.metadata_record = TitleMetadata(
        display_title=title.local_title, episode_count=count,
        metadata_provider="anilist", metadata_external_id=external_id,
    )
    title.external_links.append(ExternalTitleLink(
        provider="anilist", external_id=external_id, match_method="manual_search",
        is_primary=True, is_manual=True, verified_at=utc_now(),
        lifecycle_state="active",
    ))


def add_video(title, filename, *, subfolder="", **fields):
    folder = f"{title.relative_root_path}/{subfolder}" if subfolder else title.relative_root_path
    video = Video(
        relative_path=f"{folder}/{filename}", root_folder="Anime", filename=filename,
        size=1, mtime_ns=1, file_type=fields.pop("file_type", "episode"),
        catalog_title=title, catalog_collection=title.collection, **fields,
    )
    if (session := object_session(title)) is not None:
        session.add(video)
    return video


def build(session, specs, *, name="Show"):
    """specs: (key, season, part, sources, metadata_count) in structural order."""
    collection = CatalogCollection(
        local_title=name, normalized_local_title=name.casefold(),
        relative_root_path=f"Anime/{name}",
    )
    titles = {}
    for index, (key, season, part, sources, count) in enumerate(specs, 1):
        title = CatalogTitle(
            collection=collection, local_title=f"{name} {key}",
            normalized_local_title=f"{name} {key}".casefold(),
            relative_root_path=f"Anime/{name}/{key}", part_type="season",
            season_number=season, hierarchy_manual_override=True,
            part_type_manual="season", season_number_manual=season,
            part_number_manual=part, season_label_manual=f"S{season}",
            hierarchy_verified_at=utc_now(),
        )
        for number in sources:
            add_video(title, f"{name} - {number:02}.mkv")
        if count is not None:
            attach_metadata(title, count, f"{name}-{index}")
        titles[key] = title
    session.add(collection)
    session.flush()
    finalize_hierarchy_write([collection])
    session.commit()
    return collection, titles


def numbers(title):
    return sorted(
        video.season_episode_number for video in title.videos
        if video.season_episode_number is not None
    )


def absolutes(title):
    return sorted(
        (video.absolute_episode_number for video in title.videos),
        key=lambda value: (value is None, value or 0),
    )


def fresh(engine, title_id):
    with Session(engine) as session:
        title = session.get(CatalogTitle, title_id)
        return {
            "mode": title.numbering_mode, "offset": title.episode_start_offset,
            "manual": title.numbering_manual,
            "verified": title.numbering_verified_at is not None,
            "season": numbers(title), "absolute": absolutes(title),
            "external": sorted(
                (video.external_episode_number for video in title.videos),
                key=lambda value: (value is None, value or 0),
            ),
            "sources": sorted({video.episode_number_source for video in title.videos}),
            "overrides": sorted(
                video.episode_number_manual_override for video in title.videos
                if video.episode_number_manual_override is not None
            ),
        }


def confirm(session, title_id):
    title = session.get(CatalogTitle, title_id)
    evaluation = evaluate_part_local_numbering(title)
    assert evaluation is not None and evaluation.proposal is not None, evaluation
    proposal = apply_part_local_numbering(
        session, title_id, expected_fingerprint=evaluation.proposal.fingerprint,
    )
    session.commit()
    return proposal


@pytest.fixture
def engine(tmp_path):
    value = make_engine(f"sqlite:///{tmp_path / 'part-local.db'}")
    Base.metadata.create_all(value)
    yield value
    value.dispose()


@pytest.fixture
def session(engine):
    with make_session_factory(engine)() as value:
        yield value


def record_dml(engine):
    statements = []

    def record(_connection, _cursor, statement, *_args):
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    return statements, record


# --- 1-8: projection -------------------------------------------------------------

def test_standalone_season_is_unchanged_and_not_a_candidate(engine, session):
    _, titles = build(session, [("s1", 1, None, range(1, 13), 12)])
    assert evaluate_part_local_numbering(titles["s1"]) is None
    assert fresh(engine, titles["s1"].id)["season"] == list(range(1, 13))


def test_part_one_offset_zero_anchors_existing_numbers(engine, session):
    _, titles = build(session, [
        ("p1", 1, 1, range(1, 13), 12), ("p2", 1, 2, range(13, 25), 12),
    ])
    proposal = confirm(session, titles["p1"].id)
    assert (proposal.source_offset, proposal.changes_canonical) == (0, False)
    state = fresh(engine, titles["p1"].id)
    assert (state["mode"], state["offset"], state["manual"], state["verified"]) == (
        PART_LOCAL_NUMBERING_MODE, 0, True, True,
    )
    assert state["season"] == list(range(1, 13))
    assert state["absolute"] == list(range(1, 13))
    assert state["sources"] == ["part_local"]
    assert state["overrides"] == []


@pytest.mark.parametrize("sources,offset", [
    (range(13, 25), 12), (range(13, 24), 12), (range(26, 39), 25), (range(39, 51), 38),
])
def test_part_two_source_numbers_become_part_local(engine, session, sources, offset):
    # P1 without metadata leaves P2 on continuing/source numbers today.
    _, titles = build(session, [
        ("p1", 1, 1, range(1, sources.start), None), ("p2", 1, 2, sources, None),
    ])
    assert fresh(engine, titles["p2"].id)["season"] == list(sources)
    proposal = confirm(session, titles["p2"].id)
    assert proposal.source_offset == offset
    assert [(row.current_episode, row.proposed_episode) for row in proposal.rows] == [
        (number, number - offset) for number in sources
    ]
    state = fresh(engine, titles["p2"].id)
    assert (state["mode"], state["offset"]) == (PART_LOCAL_NUMBERING_MODE, offset)
    assert state["season"] == list(range(1, len(sources) + 1))
    assert state["overrides"] == []


def test_absolute_projection_uses_preceding_count_not_source_offset(engine, session):
    _, titles = build(session, [
        ("p1", 1, 1, range(1, 11), 10), ("p2", 1, 2, range(13, 25), 12),
    ])
    with Session(engine) as other:
        title = other.get(CatalogTitle, titles["p2"].id)
        set_title_numbering(title, PART_LOCAL_NUMBERING_MODE, 12)
        finalize_hierarchy_write([title.collection])
        other.commit()
    state = fresh(engine, titles["p2"].id)
    assert state["season"] == list(range(1, 13))
    # The official P1 count (10) is the base; the source offset (12) is not.
    assert state["absolute"] == list(range(11, 23))


def test_unknown_preceding_count_leaves_absolute_empty(engine, session):
    _, titles = build(session, [
        ("p1", 1, 1, range(1, 13), None), ("p2", 1, 2, range(13, 25), None),
    ])
    confirm(session, titles["p1"].id)
    confirm(session, titles["p2"].id)
    assert fresh(engine, titles["p1"].id)["absolute"] == list(range(1, 13))
    state = fresh(engine, titles["p2"].id)
    assert state["season"] == list(range(1, 13))
    assert state["absolute"] == [None] * 12


# --- 9-14: proposal gate -------------------------------------------------------

def test_split_confirmation_alone_does_not_renumber_then_offers_proposal(engine, session):
    collection, titles = build(session, [
        ("a", 1, None, range(1, 13), None), ("b", 1, None, range(13, 25), None),
    ])
    confirm_existing_split_season_parts(
        session, collection.id, season_number=1,
        title_ids=[titles["a"].id, titles["b"].id],
    )
    session.commit()
    state = fresh(engine, titles["b"].id)
    assert (state["mode"], state["season"]) == ("unknown", list(range(13, 25)))
    with Session(engine) as other:
        title = other.get(CatalogTitle, titles["b"].id)
        assert title.effective_part_number == 2
        evaluation = evaluate_part_local_numbering(title)
        assert evaluation.proposal is not None and evaluation.proposal.source_offset == 12


def test_safe_proposal_is_read_only_and_describes_the_mapping(engine, session):
    # 36 preceding episodes exceed source 13, so P2 stays on continuing numbers.
    _, titles = build(session, [
        ("s1", 1, None, range(1, 25), 24), ("p1", 2, 1, range(1, 13), 12),
        ("p2", 2, 2, range(13, 25), None),
    ])
    statements, record = record_dml(engine)
    try:
        with Session(engine) as other:
            evaluation = evaluate_part_local_numbering(other.get(CatalogTitle, titles["p2"].id))
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert statements == []
    proposal = evaluation.proposal
    assert (proposal.season_number, proposal.part_number, proposal.current_mode) == (2, 2, "unknown")
    assert proposal.proposed_mode == PART_LOCAL_NUMBERING_MODE
    assert proposal.logical_episode_count == 12 and proposal.physical_video_count == 12
    assert proposal.mapping_label == "E13–E24 → E1–E12"
    assert proposal.rows[0].absolute_before == (49,)
    assert proposal.rows[0].absolute_after == 37 and proposal.rows[-1].absolute_after == 48
    assert proposal.rows[0].external_after is None
    assert len(proposal.fingerprint) == 64


def test_gap_blocks_proposal(engine, session):
    _, titles = build(session, [
        ("p1", 1, 1, range(1, 13), None), ("p2", 1, 2, [13, 14, *range(16, 25)], None),
    ])
    evaluation = evaluate_part_local_numbering(titles["p2"])
    assert evaluation.proposal is None and evaluation.review_reason == HIERARCHY_ISSUE_REASON


def test_collision_blocks_proposal(engine, session):
    _, titles = build(session, [
        ("p1", 1, 1, range(1, 13), None), ("p2", 1, 2, range(13, 25), None),
    ])
    add_video(titles["p2"], "Show - 15.mkv", subfolder="copy")
    session.flush()
    finalize_hierarchy_write([titles["p2"].collection])
    session.commit()
    evaluation = evaluate_part_local_numbering(titles["p2"])
    assert evaluation.proposal is None
    assert evaluation.review_reason in {HIERARCHY_ISSUE_REASON, AMBIGUOUS_IDENTITY_REASON}


def test_incompatible_manual_number_is_review_and_preserved(engine, session):
    _, titles = build(session, [
        ("p1", 1, 1, range(1, 13), None), ("p2", 1, 2, range(13, 25), None),
    ])
    video = next(v for v in titles["p2"].videos if v.filename == "Show - 13.mkv")
    set_video_episode_override(video, 13)
    finalize_hierarchy_write([titles["p2"].collection])
    session.commit()
    evaluation = evaluate_part_local_numbering(titles["p2"])
    assert evaluation.proposal is None and evaluation.review_reason == MANUAL_CONFLICT_REASON
    assert fresh(engine, titles["p2"].id)["overrides"] == [13]


def test_existing_title_numbering_authority_is_review(engine, session):
    _, titles = build(session, [
        ("p1", 1, 1, range(1, 13), None), ("p2", 1, 2, range(13, 25), None),
    ])
    set_title_numbering(titles["p2"], "season_local", None)
    finalize_hierarchy_write([titles["p2"].collection])
    session.commit()
    evaluation = evaluate_part_local_numbering(titles["p2"])
    assert evaluation.proposal is None and evaluation.review_reason == EXISTING_AUTHORITY_REASON


def test_recap_needing_coordinate_conversion_is_review(engine, session):
    _, titles = build(session, [
        ("p1", 1, 1, range(1, 13), None), ("p2", 1, 2, range(13, 25), None),
    ])
    add_video(
        titles["p2"], "Show - 18.5.mkv", content_type_manual="recap",
        recap_episode_number_manual_tenths=185,
    )
    session.flush()
    finalize_hierarchy_write([titles["p2"].collection])
    session.commit()
    evaluation = evaluate_part_local_numbering(titles["p2"])
    assert evaluation.proposal is None and evaluation.review_reason == RECAP_REASON


def test_part_relative_recap_is_kept_when_numbers_do_not_change(engine, session):
    _, titles = build(session, [
        ("s1", 1, None, range(1, 25), 24),
        ("p1", 2, 1, range(25, 37), 12), ("p2", 2, 2, range(37, 49), 12),
    ])
    add_video(
        titles["p1"], "Show - 36.5.mkv", content_type_manual="recap",
        recap_episode_number_manual_tenths=125,
    )
    session.flush()
    finalize_hierarchy_write([titles["p1"].collection])
    session.commit()
    proposal = confirm(session, titles["p1"].id)
    assert (proposal.source_offset, proposal.changes_canonical) == (24, False)
    with Session(engine) as other:
        recap = other.scalar(select(Video).where(Video.filename == "Show - 36.5.mkv"))
        assert recap.recap_episode_number_manual_tenths == 125
        assert recap.season_episode_number is None


# --- 15-18: duplicates, variants, Media Parts, supplementary -------------------

def test_valid_duplicate_copy_follows_primary_and_stays_valid(engine, session):
    _, titles = build(session, [
        ("p1", 1, 1, range(1, 13), None), ("p2", 1, 2, range(13, 25), None),
    ])
    primary = next(v for v in titles["p2"].videos if v.filename == "Show - 15.mkv")
    copy = add_video(titles["p2"], "Show - 15.mkv", subfolder="copy")
    session.flush()
    finalize_hierarchy_write([titles["p2"].collection])
    set_duplicate_group_primary([primary, copy], primary)
    session.flush()
    finalize_hierarchy_write([titles["p2"].collection])
    session.commit()
    proposal = confirm(session, titles["p2"].id)
    row = next(item for item in proposal.rows if item.current_episode == 15)
    assert row.duplicate_copy_count == 1 and row.physical_count == 1
    with Session(engine) as other:
        videos = other.scalars(select(Video).where(Video.filename == "Show - 15.mkv")).all()
        assert sorted(v.season_episode_number for v in videos) == [3, 3]
        secondary = next(v for v in videos if v.duplicate_of_video_id is not None)
        assert duplicate_relation_state(secondary) == DuplicateRelationState.VALID


def test_confirmed_variant_lanes_remain_separate_representations(engine, session):
    _, titles = build(session, [
        ("p1", 1, 1, range(1, 13), None), ("p2", 1, 2, [], None),
    ])
    p2 = titles["p2"]
    tv = VideoVariantGroup(catalog_title=p2, manual_label="TV", release_source="tv")
    bd = VideoVariantGroup(catalog_title=p2, manual_label="BD", release_source="bd")
    session.add_all([tv, bd])
    for number in range(13, 25):
        add_video(p2, f"Show - {number:02}.mkv", subfolder="tv", video_variant_group=tv)
        add_video(p2, f"Show - {number:02}.mkv", subfolder="bd", video_variant_group=bd)
    session.flush()
    finalize_hierarchy_write([p2.collection])
    session.commit()
    proposal = confirm(session, p2.id)
    assert proposal.physical_video_count == 24 and proposal.logical_episode_count == 12
    with Session(engine) as other:
        title = other.get(CatalogTitle, p2.id)
        partitions = logical_episode_partitions(list(title.videos), catalog_title=title)
        assert [p.identity.season_episode_number for p in partitions] == list(range(1, 13))
        assert all(len(p.confirmed_variants) == 2 and not p.unassigned_videos for p in partitions)


def test_media_parts_keep_their_segment_numbers(engine, session):
    _, titles = build(session, [
        ("p1", 1, 1, range(1, 13), None), ("p2", 1, 2, [13, 14, *range(16, 25)], None),
    ])
    add_video(titles["p2"], "Show - 15.mkv", subfolder="cd1", media_part_number=1)
    add_video(titles["p2"], "Show - 15.mkv", subfolder="cd2", media_part_number=2)
    session.flush()
    finalize_hierarchy_write([titles["p2"].collection])
    session.commit()
    proposal = confirm(session, titles["p2"].id)
    assert next(row for row in proposal.rows if row.current_episode == 15).physical_count == 2
    with Session(engine) as other:
        segments = other.scalars(select(Video).where(Video.media_part_number.is_not(None))).all()
        assert sorted((v.media_part_number, v.season_episode_number) for v in segments) == [(1, 3), (2, 3)]


def test_supplementary_ordinal_and_type_are_unchanged(engine, session):
    _, titles = build(session, [
        ("p1", 1, 1, range(1, 13), None), ("p2", 1, 2, range(13, 25), None),
    ])
    add_video(titles["p2"], "Show - NCOP1.mkv", file_type="ncop")
    session.flush()
    finalize_hierarchy_write([titles["p2"].collection])
    session.commit()
    confirm(session, titles["p2"].id)
    with Session(engine) as other:
        ncop = other.scalar(select(Video).where(Video.filename == "Show - NCOP1.mkv"))
        state = supplementary_ordinal(ncop, ncop.catalog_title)
        assert (state.supplementary_type, state.number) == ("ncop", 1)
        assert (ncop.file_type, ncop.content_type_manual, ncop.season_episode_number) == ("ncop", None, None)


# --- 19-22: stability, metadata ---------------------------------------------

def test_real_rescan_keeps_part_local_authority(tmp_path: Path, monkeypatch):
    root = tmp_path / "media"
    for folder, sources in (("First", range(1, 13)), ("Second", range(13, 25))):
        (root / "Show" / folder).mkdir(parents=True)
        for number in sources:
            (root / "Show" / folder / f"Show - {number:02}.mkv").write_bytes(b"x")
    files = sorted(root.rglob("*.mkv"))
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in files}
    monkeypatch.setattr("app.scanner.service.probe_video", lambda _, **__: PROBE_RESULT)
    engine = create_engine(f"sqlite:///{tmp_path / 'scan.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions() as db:
        scan_library(db, root)
        first = db.scalar(select(CatalogTitle).where(CatalogTitle.local_title == "First"))
        second = db.scalar(select(CatalogTitle).where(CatalogTitle.local_title == "Second"))
        for title, part in ((first, 1), (second, 2)):
            set_manual_title_hierarchy(
                title, part_type="season", season_number=1, season_label="S1",
                sort_order=None, hierarchy_verified=True, part_number=part,
            )
        db.commit()
        second_id = second.id
        assert numbers(second) == list(range(13, 25))
        confirm(db, second_id)
    with sessions() as db:
        scan_library(db, root)
        db.commit()
    state = fresh(engine, second_id)
    assert (state["mode"], state["offset"]) == (PART_LOCAL_NUMBERING_MODE, 12)
    assert state["season"] == list(range(1, 13))
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in files} == before


def test_repeated_finalization_is_semantically_idle(engine, session):
    collection, titles = build(session, [
        ("p1", 1, 1, range(1, 13), 12), ("p2", 1, 2, range(13, 25), 12),
    ])
    confirm(session, titles["p2"].id)
    statements, record = record_dml(engine)
    try:
        with make_session_factory(engine)() as other:
            loaded = other.scalar(select(CatalogCollection).options(
                selectinload(CatalogCollection.titles).selectinload(CatalogTitle.videos),
            ).where(CatalogCollection.id == collection.id))
            finalize_hierarchy_write([loaded])
            finalize_hierarchy_write([loaded])
            other.flush()
            other.commit()
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert statements == []


def test_metadata_completion_count_and_external_projection(engine, session):
    _, titles = build(session, [
        ("p1", 1, 1, range(1, 13), 12), ("p2", 1, 2, range(13, 25), 12),
    ])
    with Session(engine) as other:
        title = other.get(CatalogTitle, titles["p2"].id)
        before = (
            resolve_metadata_completion(title, title.videos).state,
            local_episode_count_evidence(title).count,
        )
    confirm(session, titles["p2"].id)
    with Session(engine) as other:
        title = other.get(CatalogTitle, titles["p2"].id)
        after = (
            resolve_metadata_completion(title, title.videos).state,
            local_episode_count_evidence(title).count,
        )
        finalize_hierarchy_write([title.collection])
        other.flush()
        stable_external = sorted(v.external_episode_number for v in title.videos)
    assert before == after == ("confirmed", 12)
    assert fresh(engine, titles["p2"].id)["external"] == list(range(1, 13)) == stable_external


# --- 23-25: routes, staleness, atomicity -----------------------------------------

def _web_app(tmp_path):
    app = create_app(Settings(
        anime_path=tmp_path / "media", database_url=f"sqlite:///{tmp_path / 'web.db'}",
        metadata_download_artwork=False, metadata_artwork_directory=tmp_path / "artwork",
    ))
    engine = app.state.sessions.kw["bind"]
    Base.metadata.create_all(engine)
    with app.state.sessions() as db:
        collection, titles = build(db, [
            ("s1", 1, None, range(1, 25), 24), ("p1", 2, 1, range(1, 13), 12),
            ("p2", 2, 2, range(13, 25), 12),
        ])
        return app, engine, collection.id, titles["p2"].id


def _get(app, route_path, actual_path, *args):
    endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", None) == route_path)
    return endpoint(Request({
        "type": "http", "app": app, "method": "GET", "path": actual_path, "root_path": "",
        "scheme": "http", "query_string": b"", "headers": [],
        "server": ("testserver", 80), "client": ("testclient", 50000),
    }), *args)


def _post(app, collection_id, values):
    body = urlencode(values).encode()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    path = f"/hierarchy-review/{collection_id}/part-local-confirm"
    endpoint = next(
        route.endpoint for route in app.routes
        if getattr(route, "path", None) == "/hierarchy-review/{collection_id}/part-local-confirm"
    )
    return asyncio.run(endpoint(Request({
        "type": "http", "method": "POST", "path": path, "root_path": "", "scheme": "http",
        "query_string": b"", "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
        "server": ("testserver", 80), "client": ("testclient", 50000), "app": app,
    }, receive), collection_id))


def test_get_preview_is_no_write_and_route_confirm_applies(tmp_path):
    app, engine, collection_id, title_id = _web_app(tmp_path)
    statements, record = record_dml(engine)
    try:
        detail = _get(app, "/hierarchy-review/{collection_id}", f"/hierarchy-review/{collection_id}", collection_id)
        edit = _get(
            app, "/hierarchy-review/{collection_id}/titles/{catalog_title_id}",
            f"/hierarchy-review/{collection_id}/titles/{title_id}", collection_id, title_id,
        )
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert statements == []
    body = detail.body.decode()
    assert "Návrh Part-lokálního číslování" in body
    assert "zdrojové E13 → Part E01" in body and "E13–E24 → E1–E12" in body
    assert "Návrh Part-lokálního číslování" in edit.body.decode()
    with Session(engine) as other:
        fingerprint = evaluate_part_local_numbering(
            other.get(CatalogTitle, title_id)
        ).proposal.fingerprint
    with pytest.raises(HTTPException):
        _post(app, collection_id, {
            "catalog_title_id": title_id, "expected_fingerprint": fingerprint,
        })
    assert fresh(engine, title_id)["mode"] == "unknown"
    response = _post(app, collection_id, {
        "catalog_title_id": title_id, "expected_fingerprint": fingerprint,
        "confirm_part_local": "true",
    })
    assert response.status_code == 303
    assert fresh(engine, title_id)["season"] == list(range(1, 13))
    label_page = _get(
        app, "/hierarchy-review/{collection_id}/titles/{catalog_title_id}",
        f"/hierarchy-review/{collection_id}/titles/{title_id}", collection_id, title_id,
    ).body.decode()
    assert "Offset zdrojového číslování" in label_page
    assert "Návrh Part-lokálního číslování" not in label_page


def test_stale_fingerprint_is_rejected_without_changes(engine, session):
    _, titles = build(session, [
        ("p1", 1, 1, range(1, 13), None), ("p2", 1, 2, range(13, 25), None),
    ])
    proposal = evaluate_part_local_numbering(titles["p2"]).proposal
    add_video(titles["p2"], "Show - NCOP1.mkv", file_type="ncop")
    session.flush()
    finalize_hierarchy_write([titles["p2"].collection])
    session.commit()
    with pytest.raises(ValueError, match="zastaralý"):
        apply_part_local_numbering(session, titles["p2"].id, expected_fingerprint=proposal.fingerprint)
    session.rollback()
    with pytest.raises(ValueError, match="zastaralý"):
        apply_part_local_numbering(session, titles["p2"].id, expected_fingerprint="x" * 64)
    session.rollback()
    state = fresh(engine, titles["p2"].id)
    assert (state["mode"], state["season"]) == ("unknown", list(range(13, 25)))


def test_confirm_failure_rolls_back_atomically(engine, session, monkeypatch):
    _, titles = build(session, [
        ("p1", 1, 1, range(1, 13), None), ("p2", 1, 2, range(13, 25), None),
    ])
    proposal = evaluate_part_local_numbering(titles["p2"]).proposal
    import app.hierarchy_evaluation as hierarchy_evaluation
    real = hierarchy_evaluation.finalize_hierarchy_write

    def failing(collections, **kwargs):
        real(collections, **kwargs)
        raise ValueError("simulovaná chyba po přepočtu")

    monkeypatch.setattr(hierarchy_evaluation, "finalize_hierarchy_write", failing)
    with pytest.raises(ValueError, match="simulovaná"):
        apply_part_local_numbering(session, titles["p2"].id, expected_fingerprint=proposal.fingerprint)
    # Even a caller that commits afterwards cannot persist a partial apply.
    session.commit()
    state = fresh(engine, titles["p2"].id)
    assert (state["mode"], state["offset"], state["season"]) == ("unknown", None, list(range(13, 25)))


# --- definitions / edit round-trip ---------------------------------------------

def test_part_local_edit_and_definitions_round_trip(engine, session):
    collection, titles = build(session, [
        ("p1", 1, 1, range(1, 13), None), ("p2", 1, 2, range(13, 25), None),
        ("s2", 2, None, range(1, 13), None),
    ])
    with pytest.raises(ValueError, match="číslo Part"):
        apply_title_numbering_edit(session, titles["s2"], PART_LOCAL_NUMBERING_MODE, "0")
    session.rollback()
    with pytest.raises(ValueError, match="offset"):
        apply_title_numbering_edit(session, titles["p2"], PART_LOCAL_NUMBERING_MODE, "")
    session.rollback()
    apply_title_numbering_edit(session, titles["p2"], PART_LOCAL_NUMBERING_MODE, "12")
    session.commit()
    assert fresh(engine, titles["p2"].id)["season"] == list(range(1, 13))
    with Session(engine) as other:
        rows = simple_definition_rows(other.get(CatalogCollection, collection.id))
    definitions = parse_simple_definitions(rows)
    part_two = next(item for item in definitions if item.title_id == titles["p2"].id)
    assert (part_two.numbering_mode, part_two.episode_start_offset) == (PART_LOCAL_NUMBERING_MODE, 12)
    with pytest.raises(ValueError, match="Part-lokální"):
        parse_manual_definitions(
            '[{"local_title": "X", "part_type_manual": "season", "season_number_manual": 1, '
            '"numbering_mode": "part_local"}]'
        )


# --- 26-28: production-shaped cases ----------------------------------------------

def test_shokugeki_shaped_s3p2_becomes_part_local_with_consistent_absolute(engine, session):
    _, titles = build(session, [
        ("s1", 1, None, range(1, 25), 24), ("s2", 2, None, range(1, 14), 13),
        ("s3p1", 3, 1, range(1, 13), 12), ("s3p2", 3, 2, range(13, 25), 12),
        ("s4", 4, None, range(1, 13), 12), ("s5", 5, None, range(1, 14), 13),
    ], name="Shokugeki")
    before = {key: fresh(engine, title.id) for key, title in titles.items()}
    assert before["s3p2"]["season"] == list(range(13, 25))
    assert before["s3p2"]["absolute"] == list(range(62, 74)) == before["s4"]["absolute"]
    proposal = confirm(session, titles["s3p2"].id)
    assert (proposal.source_offset, proposal.rows[0].absolute_after) == (12, 50)
    after = {key: fresh(engine, title.id) for key, title in titles.items()}
    assert after["s3p2"]["season"] == list(range(1, 13))
    assert after["s3p2"]["absolute"] == list(range(50, 62))
    assert after["s3p2"]["external"] == list(range(1, 13))
    for key in ("s1", "s2", "s3p1", "s4", "s5"):
        assert after[key] == before[key]


def test_sao_shaped_s4p2_becomes_part_local_with_consistent_absolute(engine, session):
    _, titles = build(session, [
        ("s1", 1, None, range(1, 26), 25), ("s2", 2, None, range(1, 25), 24),
        ("s3", 3, None, range(1, 25), 24), ("s4p1", 4, 1, range(1, 13), 12),
        ("s4p2", 4, 2, range(13, 24), 11),
    ], name="SAO")
    before = {key: fresh(engine, title.id) for key, title in titles.items()}
    assert before["s4p2"]["absolute"] == list(range(98, 109))
    confirm(session, titles["s4p2"].id)
    after = {key: fresh(engine, title.id) for key, title in titles.items()}
    assert after["s4p2"]["season"] == list(range(1, 12))
    assert after["s4p2"]["absolute"] == list(range(86, 97))
    for key in ("s1", "s2", "s3", "s4p1"):
        assert after[key] == before[key]


A_STAR_SHAPES = {
    "genjitsu_248": ([("p1", 1, 1, range(1, 14), 13)], ("p2", 1, 2, range(14, 27), 13), 13),
    "mushoku_300": ([("p1", 1, 1, range(1, 12), 11)], ("p2", 1, 2, range(12, 24), 12), 11),
    "rezero_260": ([("s1", 1, None, range(1, 26), 25)], ("s2p1", 2, 1, range(26, 39), 13), 25),
    "rezero_261": (
        [("s1", 1, None, range(1, 26), 25), ("s2p1", 2, 1, range(26, 39), 13)],
        ("s2p2", 2, 2, range(39, 51), 12), 38,
    ),
    "spy_264": ([("p1", 1, 1, range(1, 13), 12)], ("p2", 1, 2, range(13, 26), 13), 12),
    "slime_267": ([("s1", 1, None, range(1, 25), 24)], ("s2p1", 2, 1, range(25, 37), 12), 24),
    "slime_268": (
        [("s1", 1, None, range(1, 25), 24), ("s2p1", 2, 1, range(25, 37), 12)],
        ("s2p2", 2, 2, range(37, 49), 12), 36,
    ),
}


@pytest.mark.parametrize("shape", sorted(A_STAR_SHAPES))
def test_a_star_titles_keep_canonical_numbers_after_preceding_metadata_changes(engine, session, shape):
    preceding, target, offset = A_STAR_SHAPES[shape]
    _, titles = build(session, [*preceding, target])
    key = target[0]
    count = len(target[3])
    assert fresh(engine, titles[key].id)["season"] == list(range(1, count + 1))
    proposal = confirm(session, titles[key].id)
    assert (proposal.source_offset, proposal.changes_canonical) == (offset, False)
    previous = titles[preceding[-1][0]]
    with make_session_factory(engine)() as other:
        prev = other.get(CatalogTitle, previous.id)
        other.get(TitleMetadata, prev.id).episode_count += 1
        finalize_hierarchy_write([prev.collection])
        other.commit()
    shifted = fresh(engine, titles[key].id)
    assert shifted["season"] == list(range(1, count + 1))
    assert shifted["absolute"][0] == offset + 2
    with make_session_factory(engine)() as other:
        unlink_title_metadata(other, other.get(CatalogTitle, previous.id))
        other.commit()
    unlinked = fresh(engine, titles[key].id)
    assert unlinked["season"] == list(range(1, count + 1))
    assert unlinked["absolute"] == [None] * count
    assert (unlinked["mode"], unlinked["offset"]) == (PART_LOCAL_NUMBERING_MODE, offset)


# --- absolute sequence start on the Part axis -------------------------------------

def build_structure(session, specs, *, name="Show"):
    """Like ``build`` with an explicit structural type and numbering authority.

    specs: (key, part_type, season, part, sources, metadata_count, mode, offset)
    in structural order.
    """
    collection = CatalogCollection(
        local_title=name, normalized_local_title=name.casefold(),
        relative_root_path=f"Anime/{name}",
    )
    titles = {}
    for index, (key, part_type, season, part, sources, count, mode, offset) in enumerate(
        specs, 1,
    ):
        title = CatalogTitle(
            collection=collection, local_title=f"{name} {key}",
            normalized_local_title=f"{name} {key}".casefold(),
            relative_root_path=f"Anime/{name}/{key}", part_type=part_type,
            season_number=season, hierarchy_manual_override=True,
            part_type_manual=part_type, season_number_manual=season,
            part_number_manual=part,
            season_label_manual=f"S{season}" if part_type != "part" else None,
            hierarchy_verified_at=utc_now(),
        )
        for number in sources:
            add_video(title, f"{name} - {number:02}.mkv")
        if count is not None:
            attach_metadata(title, count, f"{name}-{index}")
        if mode is not None:
            set_title_numbering(title, mode, offset)
        titles[key] = title
    session.add(collection)
    session.flush()
    finalize_hierarchy_write([collection])
    session.commit()
    return collection, titles


NONE_3 = [None, None, None]
ABSOLUTE_START_CASES = {
    # Safe starts and known axes keep working.
    "season_1_without_part": (
        [("s1", "season", 1, None, range(1, 4), None, None, None)], {"s1": [1, 2, 3]},
    ),
    "isolated_s1p1": (
        [("p1", "season", 1, 1, range(1, 4), None, None, None)], {"p1": [1, 2, 3]},
    ),
    "known_season_count_then_s2": (
        [
            ("s1", "season", 1, None, range(1, 4), 3, None, None),
            ("s2", "season", 2, None, range(1, 4), None, None, None),
        ],
        {"s1": [1, 2, 3], "s2": [4, 5, 6]},
    ),
    "known_p1_count_then_season_typed_p2_local": (
        [
            ("p1", "season", 1, 1, range(1, 4), 3, None, None),
            ("p2", "season", 1, 2, range(1, 4), None, None, None),
        ],
        {"p1": [1, 2, 3], "p2": [4, 5, 6]},
    ),
    "known_p1_count_then_season_typed_p2_continuing": (
        [
            ("p1", "season", 1, 1, range(1, 4), 3, None, None),
            ("p2", "season", 1, 2, range(4, 7), None, None, None),
        ],
        {"p1": [1, 2, 3], "p2": [4, 5, 6]},
    ),
    "explicit_offset_on_season_typed_p2": (
        [("p2", "season", 1, 2, range(1, 4), None, "season_local", 3)],
        {"p2": [4, 5, 6]},
    ),
    # A Part > 1 without a known preceding count is never a safe absolute E1.
    "isolated_season_typed_s1p2": (
        [("p2", "season", 1, 2, range(1, 4), None, None, None)], {"p2": NONE_3},
    ),
    "unknown_p1_then_season_typed_p2_local": (
        [
            ("p1", "season", 1, 1, range(1, 4), None, None, None),
            ("p2", "season", 1, 2, range(1, 4), None, None, None),
        ],
        {"p1": [1, 2, 3], "p2": NONE_3},
    ),
    "unknown_p1_then_season_typed_p2_continuing": (
        [
            ("p1", "season", 1, 1, range(1, 4), None, None, None),
            ("p2", "season", 1, 2, range(4, 7), None, None, None),
        ],
        {"p1": [1, 2, 3], "p2": NONE_3},
    ),
    "season_local_s1p2_without_offset": (
        [("p2", "season", 1, 2, range(1, 4), None, "season_local", None)],
        {"p2": NONE_3},
    ),
    "isolated_s2p1": (
        [("s2p1", "season", 2, 1, range(1, 4), None, None, None)], {"s2p1": NONE_3},
    ),
    "legacy_cour_p2": (
        [
            ("c1", "cour", 1, 1, range(1, 4), None, None, None),
            ("c2", "cour", 1, 2, range(1, 4), None, None, None),
        ],
        {"c1": [1, 2, 3], "c2": NONE_3},
    ),
    "explicit_part_p2": (
        [
            ("p1", "part", 1, 1, range(1, 4), None, None, None),
            ("p2", "part", 1, 2, range(1, 4), None, None, None),
        ],
        {"p1": [1, 2, 3], "p2": NONE_3},
    ),
    "part_local_keeps_its_own_base": (
        [
            ("p1", "season", 1, 1, range(1, 4), None, PART_LOCAL_NUMBERING_MODE, 0),
            ("p2", "season", 1, 2, range(4, 7), None, PART_LOCAL_NUMBERING_MODE, 3),
        ],
        {"p1": [1, 2, 3], "p2": NONE_3},
    ),
}


@pytest.mark.parametrize("case", sorted(ABSOLUTE_START_CASES))
def test_absolute_sequence_starts_only_where_it_is_safe(engine, session, case):
    specs, expected = ABSOLUTE_START_CASES[case]
    _, titles = build_structure(session, specs)
    assert {
        key: fresh(engine, titles[key].id)["absolute"] for key in expected
    } == expected


def test_season_typed_part_two_keeps_its_canonical_numbers(engine, session):
    _, titles = build_structure(session, [
        ("p1", "season", 1, 1, range(1, 4), None, None, None),
        ("p2", "season", 1, 2, range(1, 4), None, None, None),
    ])
    state = fresh(engine, titles["p2"].id)
    assert state["season"] == [1, 2, 3]
    assert state["absolute"] == NONE_3
    assert (state["mode"], state["offset"], state["manual"]) == ("unknown", None, False)


# --- metadata split over Part-local authority -------------------------------------

def set_metadata_count(engine, title_id, count):
    with make_session_factory(engine)() as other:
        title = other.get(CatalogTitle, title_id)
        title.metadata_record.episode_count = count
        finalize_hierarchy_write([title.collection])
        other.commit()


def split_state(engine, collection_id):
    """Every authority and projection a metadata split could touch."""
    with Session(engine) as other:
        collection = other.get(CatalogCollection, collection_id)
        return {
            "titles": sorted(
                (
                    title.id, title.numbering_mode, title.episode_start_offset,
                    title.numbering_manual, title.hierarchy_manual_override,
                    title.part_type_manual, title.season_number_manual,
                    title.part_number_manual, title.season_label_manual,
                    title.hierarchy_verified_at, title.metadata_status,
                    title.metadata_record.episode_count if title.metadata_record else None,
                    tuple(sorted(
                        (link.id, link.catalog_title_id, link.lifecycle_state,
                         link.is_primary)
                        for link in title.external_links
                    )),
                )
                for title in collection.titles
            ),
            "videos": sorted(
                (
                    video.id, video.catalog_title_id, video.local_episode_number,
                    video.season_episode_number, video.absolute_episode_number,
                    video.external_episode_number, video.episode_number_source,
                    video.episode_number_manual_override,
                )
                for video in collection.videos
            ),
        }


def split_evaluation(engine, title_id):
    with Session(engine) as other:
        evaluation = evaluate_metadata_split(other.get(CatalogTitle, title_id))
        return (
            evaluation.status,
            sorted(video.season_episode_number for video in evaluation.matching_videos),
            sorted(video.season_episode_number for video in evaluation.remaining_videos),
        )


def refused_split(engine, title_id, match):
    """Apply exactly like the split route: a ValueError rolls everything back."""
    with make_session_factory(engine)() as other:
        with pytest.raises(ValueError, match=match):
            apply_metadata_split(other, title_id, confirmed=True)
        other.rollback()


def test_offset_part_local_split_is_ambiguous_not_a_source_number_subset(engine, session):
    collection, titles = build(session, [
        ("p1", 1, 1, range(1, 13), 12), ("p2", 1, 2, range(13, 25), 12),
    ])
    confirm(session, titles["p1"].id)
    confirm(session, titles["p2"].id)
    set_metadata_count(engine, titles["p2"].id, 6)
    state = fresh(engine, titles["p2"].id)
    assert (state["mode"], state["offset"]) == (PART_LOCAL_NUMBERING_MODE, 12)
    assert state["season"] == list(range(1, 13))
    before = split_state(engine, collection.id)

    # Parser/source E13..E24 is not the Part-local E01..E12 identity; the split
    # refuses to guess instead of cutting a subset from source numbers.
    assert split_evaluation(engine, titles["p2"].id) == (
        MetadataSplitStatus.AMBIGUOUS, [], [],
    )
    refused_split(engine, titles["p2"].id, "1..N")
    assert split_state(engine, collection.id) == before


def test_offset_zero_part_local_split_preview_is_refused_atomically_on_apply(
    engine, session,
):
    collection, titles = build(session, [
        ("p1", 1, 1, range(1, 13), 12), ("p2", 1, 2, range(13, 25), 12),
    ])
    confirm(session, titles["p1"].id)
    confirm(session, titles["p2"].id)
    set_metadata_count(engine, titles["p1"].id, 6)
    before = split_state(engine, collection.id)

    assert split_evaluation(engine, titles["p1"].id) == (
        MetadataSplitStatus.RECOMMENDED, list(range(1, 7)), list(range(7, 13)),
    )
    # The subset would need a second Season 1 Part 1, so apply is refused.
    refused_split(engine, titles["p1"].id, "Part 1 je použito vícekrát")

    assert split_state(engine, collection.id) == before
    state = fresh(engine, titles["p1"].id)
    assert (state["mode"], state["offset"]) == (PART_LOCAL_NUMBERING_MODE, 0)
    assert state["season"] == list(range(1, 13))
    with Session(engine) as other:
        assert len(other.get(CatalogCollection, collection.id).titles) == 2


@pytest.mark.parametrize("sources,status,matching", [
    (range(1, 13), MetadataSplitStatus.RECOMMENDED, list(range(1, 7))),
    (range(13, 25), MetadataSplitStatus.AMBIGUOUS, []),
])
def test_split_evaluation_ignores_absolute_shift_after_preceding_count_change(
    engine, session, sources, status, matching,
):
    _, titles = build(session, [
        ("p1", 1, 1, range(1, 13), 12), ("p2", 1, 2, sources, 6),
    ])
    confirm(session, titles["p1"].id)
    confirm(session, titles["p2"].id)
    first = split_evaluation(engine, titles["p2"].id)
    assert first[:2] == (status, matching)
    absolute_before = fresh(engine, titles["p2"].id)["absolute"]

    set_metadata_count(engine, titles["p1"].id, 10)
    state = fresh(engine, titles["p2"].id)
    assert state["season"] == list(range(1, 13))
    assert state["absolute"] == list(range(11, 23)) != absolute_before
    assert split_evaluation(engine, titles["p2"].id) == first


@pytest.mark.parametrize("sources,status,matching,refusal", [
    (range(1, 13), MetadataSplitStatus.RECOMMENDED, list(range(1, 7)),
     "Part 2 je použito vícekrát"),
    (range(13, 25), MetadataSplitStatus.AMBIGUOUS, [], "1..N"),
])
def test_split_without_known_absolute_axis_uses_part_local_evidence(
    engine, session, sources, status, matching, refusal,
):
    collection, titles = build(session, [
        ("p1", 1, 1, range(1, 13), None), ("p2", 1, 2, sources, 6),
    ])
    confirm(session, titles["p1"].id)
    confirm(session, titles["p2"].id)
    state = fresh(engine, titles["p2"].id)
    assert state["season"] == list(range(1, 13))
    assert state["absolute"] == [None] * 12
    before = split_state(engine, collection.id)

    # NULL absolute is not a missing local identity, and still no automatic split.
    assert split_evaluation(engine, titles["p2"].id)[:2] == (status, matching)
    refused_split(engine, titles["p2"].id, refusal)
    assert split_state(engine, collection.id) == before
