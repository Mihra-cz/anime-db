import asyncio
from datetime import datetime, timezone
import re
from urllib.parse import urlencode

import pytest
from sqlalchemy import event, func, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload
from starlette.requests import Request

import app.external_subtitle_compatibility as compatibility_module
from app.config import Settings
from app.database import Base, make_engine, make_session_factory
from app.external_subtitle_compatibility import (
    AUTOMATIC_MATCH,
    CONFIRMED_COMPATIBLE,
    CONFIRMED_INCOMPATIBLE,
    MATCH_METHOD_FILENAME,
    MATCH_METHOD_LEGACY_BACKFILL,
    MATCH_METHOD_MANUAL,
    apply_compatibility_decision,
    backfill_legacy_external_subtitle_compatibilities,
    build_compatibility_candidate_index,
    build_compatibility_presentations,
    build_video_external_subtitle_states,
    candidate_variant_videos,
    clear_manual_decision,
    confirm_compatible,
    confirm_incompatible,
    compatibility_match_method_label,
    external_subtitle_compatibility_status,
    effective_external_subtitles_for_video,
    get_compatibility,
    preview_compatibility_decision,
    synchronize_automatic_match,
)
from app.catalog import build_catalog_results, build_video_language_profile
from app.hierarchy_rebuild import (
    apply_hierarchy_rebuild_plan,
    build_hierarchy_rebuild_plan,
)
from app.main import create_app
from app.media_check import build_media_check_evaluation, build_media_check_results
from app.migrations import migrate_schema
from app.models import (
    CatalogCollection,
    CatalogTitle,
    ExternalSubtitle,
    ExternalSubtitleCompatibility,
    ExternalTitleLink,
    InternalSubtitle,
    UnresolvedExternalSubtitle,
    Video,
    VideoVariantGroup,
)
from app.scanner import scan_library
from app.video_variants import (
    assign_video_catalog_title,
    assign_video_variant_group,
)


PROBE_RESULT = {
    "duration": 60.0,
    "video_codec": "h264",
    "width": 1920,
    "height": 1080,
    "audio": [],
    "subtitles": [],
}


def test_compatibility_evidence_uses_user_facing_czech_labels():
    assert compatibility_match_method_label(MATCH_METHOD_FILENAME) == (
        "Automatické přiřazení podle názvu"
    )
    assert compatibility_match_method_label(MATCH_METHOD_LEGACY_BACKFILL) == (
        "Historická automatická vazba"
    )
    assert compatibility_match_method_label(MATCH_METHOD_MANUAL) == (
        "Ruční rozhodnutí"
    )


def _catalog(session: Session, *, label: str = "Nande"):
    collection = CatalogCollection(
        local_title=label,
        normalized_local_title=label.casefold(),
        relative_root_path=f"Anime/{label}",
        hierarchy_status="verified",
    )
    title = CatalogTitle(
        collection=collection,
        local_title=label,
        normalized_local_title=label.casefold(),
        relative_root_path=f"Anime/{label}",
        part_type="season",
        season_number=1,
        season_label="S1",
        sort_order=1,
    )
    bd = VideoVariantGroup(
        catalog_title=title,
        manual_label="BD",
        release_source="bd",
        content_variant="uncensored",
    )
    tv = VideoVariantGroup(
        catalog_title=title,
        manual_label="TV",
        release_source="tv",
        content_variant="censored",
    )
    session.add(collection)
    session.flush()
    return collection, title, bd, tv


def _video(
    title: CatalogTitle,
    collection: CatalogCollection,
    *,
    filename: str,
    episode: int = 1,
    group: VideoVariantGroup | None = None,
) -> Video:
    return Video(
        relative_path=f"{title.relative_root_path}/{filename}",
        root_folder="Anime",
        filename=filename,
        size=episode,
        mtime_ns=episode,
        file_type="episode",
        local_episode_number=episode,
        season_episode_number=episode,
        absolute_episode_number=episode,
        episode_number_source="filename",
        episode_number_confidence=1.0,
        catalog_collection=collection,
        catalog_title=title,
        video_variant_group=group,
    )


def _variant_asset(session: Session):
    collection, title, bd_group, tv_group = _catalog(session)
    bd_video = _video(
        title,
        collection,
        filename="Nande - 01.mkv",
        group=bd_group,
    )
    tv_video = _video(
        title,
        collection,
        filename="Nande - 01 Ver.TV.mkv",
        group=tv_group,
    )
    subtitle = ExternalSubtitle(
        legacy_video=bd_video,
        relative_path="Anime/Nande/Nande - 01.ass",
        codec="ass",
        language="cs",
        normalized_language="cs",
        match_method="automatic",
    )
    session.add_all([bd_video, tv_video, subtitle])
    session.flush()
    synchronize_automatic_match(session, subtitle, bd_video)
    session.flush()
    return collection, title, bd_group, tv_group, bd_video, tv_video, subtitle


def _request(web_app, path: str) -> Request:
    return Request({
        "type": "http",
        "app": web_app,
        "method": "GET",
        "path": path,
        "root_path": "",
        "scheme": "http",
        "query_string": b"",
        "headers": [],
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
    })


def _post_request(web_app, path: str, items: list[tuple[str, str]]) -> Request:
    body = urlencode(items).encode()
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {
            "type": "http.request",
            "body": body,
            "more_body": False,
        }

    return Request({
        "type": "http",
        "app": web_app,
        "method": "POST",
        "path": path,
        "root_path": "",
        "scheme": "http",
        "query_string": b"",
        "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
    }, receive)


def _compatibility_app(tmp_path):
    web_app = create_app(Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'compatibility-ui.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))
    with web_app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())
        values = _variant_asset(session)
        unrelated_collection, unrelated_title, _, _ = _catalog(
            session, label="Unrelated"
        )
        unrelated = _video(
            unrelated_title,
            unrelated_collection,
            filename="Unrelated - 01.mkv",
        )
        session.add(unrelated)
        session.commit()
        ids = {
            "collection": values[0].id,
            "title": values[1].id,
            "bd_group": values[2].id,
            "tv_group": values[3].id,
            "bd": values[4].id,
            "tv": values[5].id,
            "subtitle": values[6].id,
            "unrelated": unrelated.id,
        }
    endpoints = {
        route.path: route.endpoint
        for route in web_app.routes
        if hasattr(route, "endpoint")
    }
    return web_app, endpoints, ids


def _preview_fingerprint(body: str) -> str:
    match = re.search(r'name="expected_fingerprint" value="([0-9a-f]+)"', body)
    assert match is not None
    return match.group(1)


def test_model_is_true_many_to_many_and_language_stays_on_asset(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'model.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        _, _, _, _, bd, tv, subtitle = _variant_asset(session)
        assert external_subtitle_compatibility_status(subtitle, bd) == AUTOMATIC_MATCH
        assert get_compatibility(subtitle, bd).verified_at is None
        assert external_subtitle_compatibility_status(subtitle, tv) is None

        confirm_compatible(session, subtitle, tv, note="same timing")
        second = ExternalSubtitle(
            legacy_video=bd,
            relative_path="Anime/Nande/Nande - 01.cs.srt",
            codec="srt",
            language="sk",
            normalized_language="sk",
        )
        session.add(second)
        session.flush()
        synchronize_automatic_match(session, second, bd)
        session.commit()

        assert len(subtitle.compatibilities) == 2
        assert len(bd.external_subtitle_compatibilities) == 2
        assert subtitle.normalized_language == "cs"
        assert get_compatibility(subtitle, tv).note == "same timing"
        assert not hasattr(ExternalSubtitleCompatibility, "language")


def test_pair_unique_status_constraints_and_human_timestamps(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'constraints.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        _, _, _, _, bd, tv, subtitle = _variant_asset(session)
        fixed = datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        compatible = confirm_compatible(
            session, subtitle, tv, note="retimed", verified_at=fixed
        )
        session.flush()
        assert (compatible.status, compatible.match_method) == (
            CONFIRMED_COMPATIBLE,
            MATCH_METHOD_MANUAL,
        )
        assert compatible.verified_at == fixed
        same = confirm_compatible(
            session, subtitle, tv, note="retimed",
            verified_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        assert same.verified_at == fixed
        incompatible = confirm_incompatible(
            session, subtitle, bd, note="timing differs", verified_at=fixed
        )
        assert incompatible.status == CONFIRMED_INCOMPATIBLE
        assert incompatible.verified_at == fixed
        session.commit()

        session.add(ExternalSubtitleCompatibility(
            external_subtitle_id=subtitle.id,
            video_id=tv.id,
            status=AUTOMATIC_MATCH,
            match_method=MATCH_METHOD_FILENAME,
        ))
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()

        title = session.get(CatalogTitle, tv.catalog_title_id)
        collection = session.get(CatalogCollection, tv.catalog_collection_id)
        extra = _video(
            title,
            collection,
            filename="Nande - 02.mkv",
            episode=2,
        )
        session.add(extra)
        session.commit()
        invalid_rows = (
            ("invalid", MATCH_METHOD_FILENAME, None),
            (AUTOMATIC_MATCH, MATCH_METHOD_FILENAME, fixed),
            (CONFIRMED_COMPATIBLE, MATCH_METHOD_MANUAL, None),
            (AUTOMATIC_MATCH, "invalid", None),
        )
        for status, method, verified_at in invalid_rows:
            session.add(ExternalSubtitleCompatibility(
                external_subtitle_id=subtitle.id,
                video_id=extra.id,
                status=status,
                match_method=method,
                verified_at=verified_at,
            ))
            with pytest.raises(IntegrityError):
                session.flush()
            session.rollback()

    constraints = inspect(engine).get_unique_constraints(
        "external_subtitle_compatibilities"
    )
    assert any(
        set(item["column_names"]) == {"external_subtitle_id", "video_id"}
        for item in constraints
    )


def test_clear_human_decision_restores_only_current_filename_evidence(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'clear.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection, title, _, _, bd, tv, subtitle = _variant_asset(session)
        confirm_incompatible(session, subtitle, bd, note="checked")
        confirm_compatible(session, subtitle, tv, note="retimed")
        session.flush()

        restored = clear_manual_decision(
            session, subtitle, bd, videos=[bd, tv]
        )
        assert restored is not None
        assert (restored.status, restored.match_method, restored.verified_at, restored.note) == (
            AUTOMATIC_MATCH,
            MATCH_METHOD_FILENAME,
            None,
            None,
        )
        assert clear_manual_decision(
            session, subtitle, tv, videos=[bd, tv]
        ) is None
        session.flush()
        assert get_compatibility(subtitle, tv) is None
        assert subtitle.video_id == bd.id
        assert (bd.catalog_title_id, tv.catalog_title_id) == (title.id, title.id)
        assert bd.video_variant_group_id is not None
        assert tv.video_variant_group_id is not None
        assert collection.id is not None


@pytest.mark.parametrize(
    ("legacy_method", "expected_status", "expected_method", "verified"),
    [
        ("automatic", AUTOMATIC_MATCH, MATCH_METHOD_LEGACY_BACKFILL, False),
        ("manual", CONFIRMED_COMPATIBLE, MATCH_METHOD_MANUAL, True),
    ],
)
def test_legacy_backfill_is_truthful_and_idempotent(
    tmp_path, legacy_method, expected_status, expected_method, verified,
):
    engine = make_engine(f"sqlite:///{tmp_path / f'backfill-{legacy_method}.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection, title, _, _ = _catalog(session)
        video = _video(title, collection, filename="Nande - 01.mkv")
        subtitle = ExternalSubtitle(
            legacy_video=video,
            relative_path="Anime/Nande/Nande - 01.ass",
            codec="ass",
            language="cs",
            normalized_language="cs",
            match_method=legacy_method,
        )
        session.add_all([video, subtitle])
        session.commit()
        assert backfill_legacy_external_subtitle_compatibilities(session) == 1
        session.flush()
        row = session.scalar(select(ExternalSubtitleCompatibility))
        assert (row.status, row.match_method) == (expected_status, expected_method)
        assert (row.verified_at is not None) is verified
        original_verified_at = row.verified_at
        assert backfill_legacy_external_subtitle_compatibilities(session) == 0
        session.flush()
        assert session.scalar(select(func.count()).select_from(
            ExternalSubtitleCompatibility
        )) == 1
        assert row.verified_at == original_verified_at


def test_migration_preserves_confirmed_authority_note_and_is_stable(tmp_path):
    database_path = tmp_path / "migration-idempotence.db"
    engine = make_engine(f"sqlite:///{database_path}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        _, _, _, _, bd, _, subtitle = _variant_asset(session)
        fixed = datetime(2025, 2, 3, 4, 5, 6, tzinfo=timezone.utc)
        row = get_compatibility(subtitle, bd)
        row.status = CONFIRMED_INCOMPATIBLE
        row.match_method = MATCH_METHOD_MANUAL
        row.verified_at = fixed
        row.note = "do not overwrite"
        session.commit()
        row_id = row.id

    migrate_schema(engine)
    migrate_schema(engine)
    with Session(engine) as session:
        row = session.get(ExternalSubtitleCompatibility, row_id)
        assert (row.status, row.match_method, row.note) == (
            CONFIRMED_INCOMPATIBLE,
            MATCH_METHOD_MANUAL,
            "do not overwrite",
        )
        assert row.verified_at.replace(tzinfo=timezone.utc) == fixed
        assert session.scalar(select(func.count()).select_from(
            ExternalSubtitleCompatibility
        )) == 1

    statements: list[str] = []

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        normalized = statement.lstrip().upper()
        if normalized.startswith(("INSERT", "UPDATE", "DELETE")):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        migrate_schema(engine)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert not any("EXTERNAL_SUBTITLE_COMPATIBILITIES" in item.upper() for item in statements)


def test_unresolved_and_invalid_legacy_state_invent_no_relationship(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'invalid-legacy.db'}")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.exec_driver_sql(
            "INSERT INTO external_subtitles "
            "(id, video_id, relative_path, codec, language, normalized_language, match_method) "
            "VALUES (1, 9999, 'Ghost/01.ass', 'ass', 'unknown', 'unknown', 'automatic')"
        )
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    with Session(engine) as session:
        assert backfill_legacy_external_subtitle_compatibilities(session) == 0
        assert session.scalar(select(func.count()).select_from(
            ExternalSubtitleCompatibility
        )) == 0
    # Unresolved assets live in a separate table and therefore cannot receive
    # an invented pair during the legacy backfill.
    migrate_schema(engine)
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(
            ExternalSubtitleCompatibility
        )) == 0


def _scan_nande(tmp_path, monkeypatch):
    plain = tmp_path / "Nande" / "Nande - 01.mkv"
    tv = tmp_path / "Nande" / "Nande - 01 Ver.TV.mkv"
    subtitle = tmp_path / "Nande" / "Nande - 01.ass"
    plain.parent.mkdir()
    plain.write_bytes(b"plain")
    tv.write_bytes(b"tv")
    subtitle.write_text("jsem tady dobře", encoding="utf-8")
    monkeypatch.setattr("app.scanner.service.probe_video", lambda *_args, **_kw: PROBE_RESULT)
    engine = make_engine(f"sqlite:///{tmp_path / 'scanner.db'}")
    Base.metadata.create_all(engine)
    sessions = make_session_factory(engine)
    with sessions() as session:
        scan_library(session, tmp_path)
    return engine, sessions, plain, tv, subtitle


def test_scanner_writes_bridge_and_one_automatic_pair_without_variant_propagation(
    tmp_path, monkeypatch,
):
    engine, sessions, _, _, _ = _scan_nande(tmp_path, monkeypatch)
    with sessions() as session:
        subtitle = session.scalar(select(ExternalSubtitle).options(
            selectinload(ExternalSubtitle.compatibilities)
        ))
        videos = list(session.scalars(select(Video).order_by(Video.filename)))
        plain = next(video for video in videos if video.filename == "Nande - 01.mkv")
        tv = next(video for video in videos if "Ver.TV" in video.filename)
        assert subtitle.video_id == plain.id
        assert [(row.video_id, row.status, row.match_method, row.verified_at)
                for row in subtitle.compatibilities] == [
            (plain.id, AUTOMATIC_MATCH, MATCH_METHOD_FILENAME, None)
        ]
        assert get_compatibility(subtitle, tv) is None

        title = plain.catalog_title
        bd_group = VideoVariantGroup(
            catalog_title=title, manual_label="BD", release_source="bd"
        )
        tv_group = VideoVariantGroup(
            catalog_title=title, manual_label="TV", release_source="tv"
        )
        plain.video_variant_group = bd_group
        tv.video_variant_group = tv_group
        session.commit()
        auto_id = subtitle.compatibilities[0].id

    with sessions() as session:
        scan_library(session, tmp_path)
    with sessions() as session:
        subtitle = session.scalar(select(ExternalSubtitle).options(
            selectinload(ExternalSubtitle.compatibilities)
        ))
        assert len(subtitle.compatibilities) == 1
        assert subtitle.compatibilities[0].id == auto_id
        assert subtitle.compatibilities[0].status == AUTOMATIC_MATCH
        assert "Ver.TV" not in subtitle.compatibilities[0].video.filename
    engine.dispose()


@pytest.mark.parametrize("status", [CONFIRMED_COMPATIBLE, CONFIRMED_INCOMPATIBLE])
def test_rescan_preserves_human_variant_authority(status, tmp_path, monkeypatch):
    _, sessions, _, _, _ = _scan_nande(tmp_path, monkeypatch)
    with sessions() as session:
        subtitle = session.scalar(select(ExternalSubtitle).options(
            selectinload(ExternalSubtitle.compatibilities)
        ))
        tv = session.scalar(select(Video).where(Video.filename.contains("Ver.TV")))
        fixed = datetime(2025, 3, 4, 5, 6, 7, tzinfo=timezone.utc)
        if status == CONFIRMED_COMPATIBLE:
            row = confirm_compatible(
                session, subtitle, tv, note="human note", verified_at=fixed
            )
        else:
            row = confirm_incompatible(
                session, subtitle, tv, note="human note", verified_at=fixed
            )
        session.commit()
        row_id = row.id

    migrate_schema(sessions.kw["bind"])
    with sessions() as session:
        scan_library(session, tmp_path)
    with sessions() as session:
        row = session.get(ExternalSubtitleCompatibility, row_id)
        assert (row.status, row.match_method, row.note) == (
            status,
            MATCH_METHOD_MANUAL,
            "human note",
        )
        assert row.verified_at.replace(tzinfo=timezone.utc) == fixed


def test_ambiguous_rescan_removes_only_automatic_evidence(tmp_path, monkeypatch):
    _, sessions, _, _, _ = _scan_nande(tmp_path, monkeypatch)
    with sessions() as session:
        subtitle = session.scalar(select(ExternalSubtitle).options(
            selectinload(ExternalSubtitle.compatibilities)
        ))
        tv = session.scalar(select(Video).where(Video.filename.contains("Ver.TV")))
        human = confirm_incompatible(session, subtitle, tv, note="different timing")
        session.commit()
        subtitle_id, human_id = subtitle.id, human.id

    ambiguous = tmp_path / "Nande" / "Nande - 01.mp4"
    ambiguous.write_bytes(b"second exact stem")
    with sessions() as session:
        scan_library(session, tmp_path)
    with sessions() as session:
        subtitle = session.get(ExternalSubtitle, subtitle_id)
        assert subtitle is not None
        assert session.get(ExternalSubtitleCompatibility, human_id).status == (
            CONFIRMED_INCOMPATIBLE
        )
        assert session.scalar(select(func.count()).select_from(
            ExternalSubtitleCompatibility
        ).where(ExternalSubtitleCompatibility.status == AUTOMATIC_MATCH)) == 0


def test_scanner_file_disappearance_cascades_relationships(tmp_path, monkeypatch):
    _, sessions, _, _, subtitle_path = _scan_nande(tmp_path, monkeypatch)
    with sessions() as session:
        subtitle_id = session.scalar(select(ExternalSubtitle.id))
    subtitle_path.unlink()
    with sessions() as session:
        scan_library(session, tmp_path)
    with sessions() as session:
        assert session.get(ExternalSubtitle, subtitle_id) is None
        assert session.scalar(select(func.count()).select_from(
            ExternalSubtitleCompatibility
        )) == 0


def test_rescan_preserves_historical_match_to_confirmed_duplicate_secondary(
    tmp_path, monkeypatch,
):
    directory = tmp_path / "Duplicate"
    directory.mkdir()
    for filename in ("Show - 01.mkv", "Show - 01 TV.mkv"):
        (directory / filename).write_bytes(b"video")
    (directory / "Show - 01 TV.ass").write_text(
        "jsem tady dobře", encoding="utf-8"
    )
    monkeypatch.setattr("app.scanner.service.probe_video", lambda *_args, **_kw: PROBE_RESULT)
    engine = make_engine(f"sqlite:///{tmp_path / 'duplicate-secondary.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        scan_library(session, tmp_path)
        primary = session.scalar(select(Video).where(
            Video.filename == "Show - 01.mkv"
        ))
        secondary = session.scalar(select(Video).where(
            Video.filename == "Show - 01 TV.mkv"
        ))
        subtitle = session.scalar(select(ExternalSubtitle))
        secondary.duplicate_of = primary
        session.commit()
        subtitle_id, secondary_id = subtitle.id, secondary.id
        scan_library(session, tmp_path)
        subtitle = session.get(ExternalSubtitle, subtitle_id)
        assert subtitle.video_id == secondary_id
        assert [(row.video_id, row.status) for row in subtitle.compatibilities] == [
            (secondary_id, AUTOMATIC_MATCH)
        ]


def test_ab_and_null_or_same_group_siblings_do_not_gain_automatic_rows(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr("app.scanner.service.probe_video", lambda *_args, **_kw: PROBE_RESULT)
    for folder, video_names, subtitle_name in (
        ("AB", ("Re Zero - 01A.mkv", "Re Zero - 01B.mkv"), "Re Zero - 01A.ass"),
        ("Null", ("Show - 01.mkv", "Show - 01 Ver.TV.mkv"), "Show - 01.ass"),
        ("Same", ("Lane - 01.mkv", "Lane - 01 Ver.TV.mkv"), "Lane - 01.ass"),
    ):
        directory = tmp_path / folder
        directory.mkdir()
        for name in video_names:
            (directory / name).write_bytes(b"video")
        (directory / subtitle_name).write_text("jsem tady dobře", encoding="utf-8")
    engine = make_engine(f"sqlite:///{tmp_path / 'topology.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        scan_library(session, tmp_path)
        null_videos = list(session.scalars(select(Video).where(
            Video.relative_path.startswith("Null/")
        )))
        null_group = VideoVariantGroup(
            catalog_title=null_videos[0].catalog_title,
            manual_label="Known",
        )
        null_videos[0].video_variant_group = null_group
        same_videos = list(session.scalars(select(Video).where(
            Video.relative_path.startswith("Same/")
        )))
        same_group = VideoVariantGroup(
            catalog_title=same_videos[0].catalog_title,
            manual_label="TV",
        )
        for video in same_videos:
            video.video_variant_group = same_group
        ab_videos = list(session.scalars(select(Video).where(
            Video.relative_path.startswith("AB/")
        ).order_by(Video.filename)))
        for video, label in zip(ab_videos, ("A", "B"), strict=True):
            video.video_variant_group = VideoVariantGroup(
                catalog_title=video.catalog_title,
                manual_label=label,
            )
        session.commit()
        scan_library(session, tmp_path)
        for path_prefix in ("AB/", "Null/", "Same/"):
            subtitle = session.scalar(select(ExternalSubtitle).options(
                selectinload(ExternalSubtitle.compatibilities)
            ).where(ExternalSubtitle.relative_path.startswith(path_prefix)))
            assert len(subtitle.compatibilities) == 1


def test_ambiguous_unresolved_subtitle_has_no_invented_compatibility(
    tmp_path, monkeypatch,
):
    directory = tmp_path / "Ambiguous"
    directory.mkdir()
    for filename in ("Show - 01.mkv", "Show - 01.mp4"):
        (directory / filename).write_bytes(b"video")
    (directory / "Show - 01.ass").write_text("jsem tady dobře", encoding="utf-8")
    monkeypatch.setattr("app.scanner.service.probe_video", lambda *_args, **_kw: PROBE_RESULT)
    engine = make_engine(f"sqlite:///{tmp_path / 'unresolved.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        scan_library(session, tmp_path)
        assert session.scalar(select(func.count()).select_from(ExternalSubtitle)) == 0
        assert session.scalar(select(func.count()).select_from(
            UnresolvedExternalSubtitle
        )) == 1
        assert session.scalar(select(func.count()).select_from(
            ExternalSubtitleCompatibility
        )) == 0


def test_candidates_are_same_logical_episode_only_and_ignore_duplicate_topology(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'candidates.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection, title, tv_group, bd_group, bd, tv, subtitle = _variant_asset(session)
        episode_two = _video(
            title, collection, filename="Nande - 02.mkv", episode=2, group=bd_group
        )
        duplicate = _video(
            title, collection, filename="Nande - 01 copy.mkv", group=tv_group
        )
        duplicate.duplicate_of = tv
        session.add_all([episode_two, duplicate])
        session.flush()
        other_collection, other_title, _, _ = _catalog(session, label="Other")
        unrelated = _video(
            other_title, other_collection, filename="Other - 01.mkv"
        )
        session.add(unrelated)
        session.flush()
        candidates = candidate_variant_videos(
            subtitle, [bd, tv, episode_two, duplicate, unrelated]
        )
        assert {item.video.id for item in candidates} == {bd.id, tv.id}
        assert {item.variant_label for item in candidates} == {
            "BD · Uncensored",
            "TV · Censored",
        }


def test_video_move_group_change_and_rebuild_preserve_exact_relationship(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'lifecycle.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        _, title, _, tv_group, bd, tv, subtitle = _variant_asset(session)
        human = confirm_compatible(session, subtitle, tv, note="manual")
        session.commit()
        human_id, subtitle_id, tv_id = human.id, subtitle.id, tv.id

        replacement = VideoVariantGroup(
            catalog_title=title,
            manual_label="WEB",
            release_source="web",
        )
        session.add(replacement)
        session.flush()
        assign_video_variant_group(tv, replacement)
        session.flush()
        assert session.scalar(select(func.count()).select_from(
            ExternalSubtitleCompatibility
        )) == 2
        assign_video_variant_group(tv, None)
        session.flush()
        assert session.get(ExternalSubtitleCompatibility, human_id).video_id == tv_id
        assert len(subtitle.compatibilities) == 2

        other_collection, other_title, _, _ = _catalog(session, label="Moved")
        assign_video_catalog_title(tv, other_title)
        tv.catalog_collection = other_collection
        session.flush()
        assert session.get(ExternalSubtitleCompatibility, human_id).video_id == tv_id
        assert session.scalar(select(func.count()).select_from(
            ExternalSubtitleCompatibility
        )) == 2
        session.commit()

        plan = build_hierarchy_rebuild_plan(session)
        if plan.has_changes and not any(item.prevents_apply for item in plan.blockers):
            apply_hierarchy_rebuild_plan(session, plan)
            session.commit()
        assert session.get(ExternalSubtitleCompatibility, human_id).video_id == tv_id
        assert session.get(ExternalSubtitle, subtitle_id) is not None
        assert session.scalar(select(func.count()).select_from(
            ExternalSubtitleCompatibility
        )) == 2
        assert tv_group.id is not None
        assert bd.id is not None


def test_target_video_delete_removes_only_its_pair_and_keeps_physical_asset(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'delete.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        _, _, _, _, bd, tv, subtitle = _variant_asset(session)
        confirm_compatible(session, subtitle, tv)
        session.commit()
        subtitle_id, bd_id, tv_id = subtitle.id, bd.id, tv.id
        session.delete(tv)
        session.commit()
        assert session.get(Video, tv_id) is None
        assert session.get(ExternalSubtitle, subtitle_id) is not None
        rows = list(session.scalars(select(ExternalSubtitleCompatibility)))
        assert [(row.video_id, row.status) for row in rows] == [
            (bd_id, AUTOMATIC_MATCH)
        ]


def test_manual_preview_confirm_clear_stale_and_cross_title_rejection(tmp_path):
    web_app, endpoints, ids = _compatibility_app(tmp_path)
    preview_endpoint = endpoints[
        "/media-check/external-subtitles/{subtitle_id}/compatibility-preview"
    ]
    confirm_endpoint = endpoints[
        "/media-check/external-subtitles/{subtitle_id}/compatibility-confirm"
    ]

    media = endpoints["/media-check"](
        _request(web_app, "/media-check"),
        subtitle="all", audio="all", q="", page=1, message=None,
    )
    rendered = media.body.decode()
    assert "Kompatibilita s video variantami" in rendered
    assert "Nande - 01.ass" in rendered
    assert "E01 · BD · Uncensored" in rendered
    assert "E01 · TV · Censored" in rendered
    assert "Automaticky přiřazeno" in rendered
    assert "Ruční rozhodnutí" in rendered
    assert "Bez ručního rozhodnutí" in rendered
    assert "Potvrdit kompatibilitu" in rendered
    assert "Potvrdit nekompatibilitu" in rendered
    assert "Historická automatická vazba" not in rendered
    assert "Neurčeno / odstranit ruční rozhodnutí" not in rendered
    assert "Zobrazit náhled" in rendered
    title_detail = endpoints["/titles/{catalog_title_id}"](
        _request(web_app, f"/titles/{ids['title']}"), ids["title"]
    ).body.decode()
    assert "compatibility: Automaticky přiřazeno" in title_detail
    assert (
        f'action="/videos/{ids["bd"]}/external-subtitles/{ids["subtitle"]}/language"'
        in title_detail
    )

    preview_request = _post_request(web_app, "", [
        ("video_id", str(ids["tv"])),
        ("decision", CONFIRMED_COMPATIBLE),
        ("note", "compatible despite release"),
        ("return_to", "/media-check"),
    ])
    preview_response = asyncio.run(preview_endpoint(
        preview_request, ids["subtitle"]
    ))
    assert preview_response.status_code == 200
    preview_body = preview_response.body.decode()
    assert "Náhled kompatibility externích titulků" in preview_body
    assert "compatible despite release" in preview_body
    assert "Aktuální stav" in preview_body
    assert "Neurčeno" in preview_body
    assert "Ruční rozhodnutí" in preview_body
    assert "Potvrdit kompatibilitu" in preview_body
    assert "Výsledný stav" in preview_body
    assert "Ručně potvrzeno kompatibilní" in preview_body
    fingerprint = _preview_fingerprint(preview_body)
    with web_app.state.sessions() as session:
        assert session.scalar(select(func.count()).select_from(
            ExternalSubtitleCompatibility
        )) == 1

    missing_confirmation = _post_request(web_app, "", [
        ("video_id", str(ids["tv"])),
        ("decision", CONFIRMED_COMPATIBLE),
        ("note", "compatible despite release"),
        ("expected_fingerprint", fingerprint),
    ])
    response = asyncio.run(confirm_endpoint(
        missing_confirmation, ids["subtitle"]
    ))
    assert response.status_code == 400
    assert "explicitně potvrdit checkboxem" in response.body.decode()

    confirm_request = _post_request(web_app, "", [
        ("video_id", str(ids["tv"])),
        ("decision", CONFIRMED_COMPATIBLE),
        ("note", "compatible despite release"),
        ("expected_fingerprint", fingerprint),
        ("confirm_compatibility", "true"),
        ("return_to", "/media-check"),
    ])
    response = asyncio.run(confirm_endpoint(confirm_request, ids["subtitle"]))
    assert response.status_code == 303
    with web_app.state.sessions() as session:
        row = session.scalar(select(ExternalSubtitleCompatibility).where(
            ExternalSubtitleCompatibility.video_id == ids["tv"]
        ))
        assert (row.status, row.note) == (
            CONFIRMED_COMPATIBLE,
            "compatible despite release",
        )
        assert row.verified_at is not None

    media_after = endpoints["/media-check"](
        _request(web_app, "/media-check"),
        subtitle="available", audio="all", q="", page=1, message=None,
    ).body.decode()
    assert "Ručně potvrzeno kompatibilní" in media_after
    assert "Sdílený fyzický subtitle asset" in media_after
    assert "Bez kompatibilních externích titulků" not in media_after
    title_after = endpoints["/titles/{catalog_title_id}"](
        _request(web_app, f"/titles/{ids['title']}"), ids["title"]
    ).body.decode()
    assert title_after.count("Nande - 01.ass") >= 2
    assert (
        f'action="/videos/{ids["tv"]}/external-subtitles/'
        f'{ids["subtitle"]}/language"' in title_after
    )

    stale_preview = _post_request(web_app, "", [
        ("video_id", str(ids["tv"])),
        ("decision", CONFIRMED_INCOMPATIBLE),
        ("note", "new decision"),
    ])
    stale_response = asyncio.run(preview_endpoint(
        stale_preview, ids["subtitle"]
    ))
    stale_fingerprint = _preview_fingerprint(stale_response.body.decode())
    with web_app.state.sessions() as session:
        session.get(Video, ids["tv"]).video_variant_group_id = None
        session.commit()
    stale_confirm = _post_request(web_app, "", [
        ("video_id", str(ids["tv"])),
        ("decision", CONFIRMED_INCOMPATIBLE),
        ("note", "new decision"),
        ("expected_fingerprint", stale_fingerprint),
        ("confirm_compatibility", "true"),
    ])
    response = asyncio.run(confirm_endpoint(stale_confirm, ids["subtitle"]))
    assert response.status_code == 400
    assert "neodpovídá aktuálnímu stavu" in response.body.decode()

    for target_id in (ids["unrelated"], 999999):
        invalid = _post_request(web_app, "", [
            ("video_id", str(target_id)),
            ("decision", CONFIRMED_COMPATIBLE),
        ])
        response = asyncio.run(preview_endpoint(invalid, ids["subtitle"]))
        assert response.status_code == 400
        assert "Změnu nelze provést" in response.body.decode()


def test_manual_clear_preserves_video_duplicate_hierarchy_metadata_and_media(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'isolated-write.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection, title, _, _, bd, tv, subtitle = _variant_asset(session)
        tv.episode_number_manual_override = 1
        tv.duplicate_of = bd
        tv.duplicate_status_manual = "suspected"
        tv.duplicate_primary_missing = False
        tv.internal_subtitles.append(InternalSubtitle(
            stream_index=2,
            codec="ass",
            language="en",
            normalized_language="en",
            title="English",
        ))
        title.external_links.append(ExternalTitleLink(
            provider="anilist",
            external_id="123",
            match_method="manual",
            is_primary=True,
            is_manual=True,
        ))
        row = confirm_compatible(session, subtitle, tv, note="manual")
        session.commit()
        snapshot = (
            tv.catalog_title_id,
            tv.catalog_collection_id,
            tv.season_episode_number,
            tv.episode_number_manual_override,
            tv.duplicate_of_video_id,
            tv.duplicate_primary_missing,
            tv.duplicate_status_manual,
            tv.relative_path,
            tv.filename,
            len(tv.internal_subtitles),
            len(title.external_links),
            subtitle.video_id,
            subtitle.manual_language,
        )
        clear_manual_decision(session, subtitle, tv, videos=[bd, tv])
        session.commit()
        assert get_compatibility(subtitle, tv) is None
        assert (
            tv.catalog_title_id,
            tv.catalog_collection_id,
            tv.season_episode_number,
            tv.episode_number_manual_override,
            tv.duplicate_of_video_id,
            tv.duplicate_primary_missing,
            tv.duplicate_status_manual,
            tv.relative_path,
            tv.filename,
            len(tv.internal_subtitles),
            len(title.external_links),
            subtitle.video_id,
            subtitle.manual_language,
        ) == snapshot
        assert session.get(ExternalSubtitleCompatibility, row.id) is None
        assert collection.id is not None


def test_media_check_completion_uses_positive_compatibility_authority(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'media-semantics.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        _, _, _, _, bd, tv, subtitle = _variant_asset(session)
        before_bd = build_media_check_evaluation(bd).subtitle_status
        before_tv = build_media_check_evaluation(tv).subtitle_status
        confirm_compatible(session, subtitle, tv, note="new M:N authority")
        session.flush()
        assert build_media_check_evaluation(bd).subtitle_status == before_bd == "available"
        assert before_tv != "available"
        assert build_media_check_evaluation(tv).subtitle_status == "available"


def test_effective_read_authority_overrides_legacy_owner_and_exposes_unknown(
    tmp_path,
):
    engine = make_engine(f"sqlite:///{tmp_path / 'effective-read.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        _, _, _, _, bd, tv, subtitle = _variant_asset(session)
        states = build_video_external_subtitle_states([bd, tv])

        assert effective_external_subtitles_for_video(bd) == (subtitle,)
        assert effective_external_subtitles_for_video(tv) == ()
        assert states[tv.id].unknown_candidate_subtitles == (subtitle,)
        assert build_media_check_evaluation(
            tv, external_subtitle_state=states[tv.id],
        ).subtitle_status == "needs_cs_sk_compatibility_unknown"

        legacy_only = ExternalSubtitle(
            legacy_video=tv,
            relative_path="Anime/Nande/Nande - 01 legacy-only.ass",
            codec="ass",
            language="unknown",
            normalized_language="unknown",
            match_method="automatic",
        )
        session.add(legacy_only)
        session.flush()
        assert legacy_only.video_id == tv.id
        assert effective_external_subtitles_for_video(tv) == ()
        assert build_video_language_profile(tv).has_cs_or_sk is False

        legacy_row = get_compatibility(subtitle, bd)
        confirm_incompatible(session, subtitle, bd, note="wrong timing")
        session.flush()
        assert subtitle.video_id == bd.id
        assert get_compatibility(subtitle, bd) is legacy_row
        assert effective_external_subtitles_for_video(bd) == ()
        assert build_video_language_profile(bd).has_cs is False


def test_one_physical_asset_can_complete_two_variants_and_catalog_filters(
    tmp_path,
):
    engine = make_engine(f"sqlite:///{tmp_path / 'shared-read.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        _, _, _, _, bd, tv, subtitle = _variant_asset(session)
        initial = build_catalog_results([bd, tv], "all").groups[0]
        assert (initial.total, initial.cs, initial.missing) == (2, 1, 1)

        confirm_compatible(session, subtitle, tv, note="same timing")
        session.flush()
        assert effective_external_subtitles_for_video(bd) == (subtitle,)
        assert effective_external_subtitles_for_video(tv) == (subtitle,)
        assert session.scalar(select(func.count()).select_from(ExternalSubtitle)) == 1

        results = build_media_check_results(
            [bd, tv], subtitle_filter="available", page_size=10,
        )
        assert {row.video.id for row in results.rows} == {bd.id, tv.id}
        assert results.subtitle_counts["available"] == 2
        assert results.subtitle_counts["unresolved"] == 0
        assert build_media_check_results(
            [bd, tv], subtitle_filter="unresolved", page_size=10,
        ).total_filtered == 0

        catalog = build_catalog_results([bd, tv], "all").groups[0]
        assert (catalog.total, catalog.cs, catalog.sk, catalog.missing) == (2, 2, 0, 0)

        subtitle.manual_language = "sk"
        assert build_video_language_profile(bd).external_subtitle_languages == {"sk"}
        assert build_video_language_profile(tv).external_subtitle_languages == {"sk"}


def test_negative_asset_does_not_mask_another_positive_asset(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'multiple-assets.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        _, _, _, _, bd, tv, first = _variant_asset(session)
        confirm_incompatible(session, first, tv)
        second = ExternalSubtitle(
            legacy_video=bd,
            relative_path="Anime/Nande/Nande - 01 alternate.ass",
            codec="ass",
            language="cs",
            normalized_language="cs",
            match_method="manual",
        )
        session.add(second)
        session.flush()
        confirm_compatible(session, second, tv)
        session.flush()

        assert effective_external_subtitles_for_video(tv) == (second,)
        assert build_video_language_profile(tv).has_cs is True
        assert build_media_check_evaluation(tv).subtitle_status == "available"


def test_compatibility_presentation_builder_indexes_library_once(
    tmp_path, monkeypatch,
):
    engine = make_engine(f"sqlite:///{tmp_path / 'candidate-scale.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        videos: list[Video] = []
        subtitle_pairs: list[tuple[ExternalSubtitle, Video]] = []
        sample: tuple[ExternalSubtitle, Video, Video, Video] | None = None
        for title_number in range(12):
            label = f"Scale-{title_number:02d}"
            collection, title, bd_group, tv_group = _catalog(
                session, label=label
            )
            for episode in range(1, 19):
                bd_video = _video(
                    title,
                    collection,
                    filename=f"{label} - {episode:02d}.mkv",
                    episode=episode,
                    group=bd_group,
                )
                tv_video = _video(
                    title,
                    collection,
                    filename=f"{label} - {episode:02d} Ver.TV.mkv",
                    episode=episode,
                    group=tv_group,
                )
                subtitle = ExternalSubtitle(
                    legacy_video=bd_video,
                    relative_path=f"Anime/{label}/{label} - {episode:02d}.ass",
                    codec="ass",
                    language="cs",
                    normalized_language="cs",
                )
                rows: list[object] = [bd_video, tv_video, subtitle]
                videos.extend((bd_video, tv_video))
                subtitle_pairs.append((subtitle, bd_video))
                if episode % 6 == 0:
                    duplicate = _video(
                        title,
                        collection,
                        filename=f"{label} - {episode:02d} copy.mkv",
                        episode=episode,
                        group=bd_group,
                    )
                    duplicate.duplicate_of = bd_video
                    rows.append(duplicate)
                    videos.append(duplicate)
                    if title_number == 0 and episode == 6:
                        sample = (subtitle, bd_video, tv_video, duplicate)
                session.add_all(rows)
            normal_video = _video(
                title,
                collection,
                filename=f"{label} - 99.mkv",
                episode=99,
            )
            session.add(normal_video)
            videos.append(normal_video)
        session.flush()
        for subtitle, legacy_video in subtitle_pairs:
            synchronize_automatic_match(session, subtitle, legacy_video)
        session.flush()

        original_identity = compatibility_module.logical_episode_identity
        identity_calls = 0

        def count_identity(video, **kwargs):
            nonlocal identity_calls
            identity_calls += 1
            return original_identity(video, **kwargs)

        monkeypatch.setattr(
            compatibility_module, "logical_episode_identity", count_identity
        )
        candidate_index = build_compatibility_candidate_index(videos)
        presentations = build_compatibility_presentations(
            videos, candidate_index=candidate_index,
        )
        subtitle_states = build_video_external_subtitle_states(
            videos, candidate_index=candidate_index,
        )

        assert len(videos) == 480
        assert len(subtitle_pairs) == 216
        assert len(presentations) == len(subtitle_pairs)
        assert identity_calls == len(videos)
        assert len(subtitle_states) == len(videos)
        assert all(len(item.candidates) == 2 for item in presentations.values())
        assert sample is not None
        subtitle, bd_video, tv_video, duplicate = sample
        assert {
            candidate.video.id
            for candidate in presentations[subtitle.id].candidates
        } == {bd_video.id, tv_video.id}
        assert duplicate.id not in {
            candidate.video.id for candidate in presentations[subtitle.id].candidates
        }


def test_media_check_compatibility_loading_has_constant_statement_count(
    tmp_path, monkeypatch,
):
    web_app, endpoints, ids = _compatibility_app(tmp_path)
    engine = web_app.state.sessions.kw["bind"]
    endpoint = endpoints["/media-check"]

    def count_queries():
        count = 0

        def increment(*_args):
            nonlocal count
            count += 1

        event.listen(engine, "before_cursor_execute", increment)
        try:
            response = endpoint(
                _request(web_app, "/media-check"),
                subtitle="all", audio="all", q="", page=1, message=None,
            )
            assert response.status_code == 200
        finally:
            event.remove(engine, "before_cursor_execute", increment)
        return count, response

    one_episode_count, _ = count_queries()
    assert one_episode_count == 14
    with web_app.state.sessions() as session:
        title = session.get(CatalogTitle, ids["title"])
        collection = session.get(CatalogCollection, ids["collection"])
        bd_group = session.get(VideoVariantGroup, ids["bd_group"])
        subtitle_pairs = []
        for episode in range(2, 242):
            video = _video(
                title,
                collection,
                filename=f"Nande - {episode:02d}.mkv",
                episode=episode,
                group=bd_group,
            )
            subtitle = ExternalSubtitle(
                legacy_video=video,
                relative_path=f"Anime/Nande/Nande - {episode:02d}.ass",
                codec="ass",
                language="cs",
                normalized_language="cs",
            )
            session.add_all([video, subtitle])
            subtitle_pairs.append((subtitle, video))
        session.flush()
        for subtitle, video in subtitle_pairs:
            synchronize_automatic_match(session, subtitle, video)
        session.commit()

    original_identity = compatibility_module.logical_episode_identity
    identity_calls = 0

    def count_identity(video, **kwargs):
        nonlocal identity_calls
        identity_calls += 1
        return original_identity(video, **kwargs)

    monkeypatch.setattr(
        compatibility_module, "logical_episode_identity", count_identity
    )
    large_count, response = count_queries()
    with web_app.state.sessions() as session:
        video_count = session.scalar(select(func.count()).select_from(Video))
        subtitle_count = session.scalar(
            select(func.count()).select_from(ExternalSubtitle)
        )

    results = response.context["results"]
    presentations = response.context["compatibility_presentations"]
    expected_page_subtitles = {
        item.id
        for row in results.rows
        for item in row.video.external_subtitles
    }
    assert large_count == one_episode_count
    assert video_count == 243
    assert subtitle_count == 241
    assert len(results.rows) == 50
    assert set(presentations) == expected_page_subtitles
    assert len(presentations) < subtitle_count
    assert identity_calls <= video_count + len(results.rows)


def test_preview_service_is_stale_protected_and_atomic(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'service-preview.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        _, _, _, _, bd, tv, subtitle = _variant_asset(session)
        preview = preview_compatibility_decision(
            subtitle,
            tv.id,
            CONFIRMED_COMPATIBLE,
            note="checked",
            videos=[bd, tv],
        )
        assert get_compatibility(subtitle, tv) is None
        session.commit()
        tv.video_variant_group_id = None
        with pytest.raises(ValueError, match="neodpovídá aktuálnímu stavu"):
            apply_compatibility_decision(
                session,
                subtitle,
                tv.id,
                CONFIRMED_COMPATIBLE,
                note="checked",
                videos=[bd, tv],
                expected_fingerprint=preview.fingerprint,
            )
        session.rollback()
        assert session.scalar(select(func.count()).select_from(
            ExternalSubtitleCompatibility
        )) == 1
