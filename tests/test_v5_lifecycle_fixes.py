"""Regrese pro poslední potvrzené V5 lifecycle defekty.

Pokrývá čtyři oddělené oblasti:

* sdílená finalizace je tolerantní reconciliation a legacy ``recap_outside_season``
  proto neblokuje nesouvisející ani opravný zápis (explicitní manual split
  zůstává strict);
* každý explicitní write surface, který spouští automatic structural inference,
  je chráněn ``strict_hierarchy_write_guard``;
* structural move zahrne i collection přiřazené části, když je redundantní
  vazba videa rozbitá;
* explicitní assignment sám nepřepne collection do manual-split režimu a
  scanner posuzuje Recap proti již odvozenému Season kontextu.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import app.hierarchy_evaluation as hierarchy_evaluation
from app.config import Settings
from app.database import Base
from app.hierarchy_authority import activate_manual_hierarchy_snapshot
from app.hierarchy_evaluation import evaluate_collection_hierarchy
from app.hierarchy_review import (
    ManualTitleDefinition,
    apply_manual_split,
    apply_single_title_confirmation,
    assign_known_videos_to_title,
    classify_videos_in_place,
    confirm_existing_split_season_parts,
    refresh_collection_state,
    set_manual_title_hierarchy,
)
from app.main import create_app
from app.manual_split import manual_split_rule_titles, manual_split_titles
from app.metadata.split import apply_metadata_split
from app.models import (
    CatalogCollection,
    CatalogTitle,
    ExternalTitleLink,
    TitleMetadata,
    Video,
    utc_now,
)
from app.numbering import set_video_episode_number_from_input
from app.scanner import scan_library


PROBE_RESULT = {
    "duration": 60.0,
    "video_codec": "h264",
    "width": 1920,
    "height": 1080,
    "audio": [],
    "subtitles": [],
}


def _issue_codes(collection: CatalogCollection) -> set[str]:
    return {
        issue.code.value
        for issue in evaluate_collection_hierarchy(
            collection, list(collection.videos),
        ).issues
    }


def _video(
    collection: CatalogCollection,
    title: CatalogTitle | None,
    filename: str,
    size: int,
    *,
    file_type: str = "episode",
    content_type_manual: str | None = None,
) -> Video:
    folder = title.relative_root_path if title is not None else collection.relative_root_path
    return Video(
        relative_path=f"{folder}/{filename}",
        root_folder="Anime",
        filename=filename,
        size=size,
        mtime_ns=size,
        file_type=file_type,
        content_type_manual=content_type_manual,
        catalog_collection=collection,
        catalog_title=title,
    )


def _season(
    collection: CatalogCollection,
    *,
    name: str = "Season 1",
    season_number: int = 1,
    sort_order: int = 0,
) -> CatalogTitle:
    title = CatalogTitle(
        collection=collection,
        local_title=name,
        normalized_local_title=name.casefold(),
        relative_root_path=f"{collection.relative_root_path}/{name}",
        numbering_mode="unknown",
    )
    activate_manual_hierarchy_snapshot(
        title,
        part_type="season",
        season_number=season_number,
        part_number=None,
        season_label=f"S{season_number}",
        sort_order=sort_order,
        verified_at=utc_now(),
    )
    return title


def _supplementary(
    collection: CatalogCollection,
    *,
    name: str,
    part_type: str,
    season_number: int | None,
    sort_order: int,
) -> CatalogTitle:
    title = CatalogTitle(
        collection=collection,
        local_title=name,
        normalized_local_title=name.casefold(),
        relative_root_path=f"{collection.relative_root_path}/{name}",
        numbering_mode="unknown",
    )
    activate_manual_hierarchy_snapshot(
        title,
        part_type=part_type,
        season_number=season_number,
        part_number=None,
        season_label=f"S{season_number}" if season_number is not None else None,
        sort_order=sort_order,
        verified_at=utc_now(),
    )
    return title


def _legacy_manual_split_collection(session: Session) -> dict[str, int]:
    """Reprodukuje potvrzený legacy stav: efektivní Recap mimo Season kontext
    v collection, která má persistentní manual-split selector.

    Odpovídá výsledku podporovaného lifecycle scan -> manual split -> scan s
    potvrzeným úbytkem epizod, po kterém direct-root title ztratil automatic
    Season a tolerantní scanner Recap ponechal na místě pro review.
    """
    collection = CatalogCollection(
        local_title="Show",
        normalized_local_title="show",
        relative_root_path="Anime/Show",
    )
    direct = CatalogTitle(
        collection=collection,
        local_title="Show",
        normalized_local_title="show",
        relative_root_path="Anime/Show",
        part_type="title",
        numbering_mode="unknown",
        episode_filename_pattern=r"\.mkv$",
    )
    episodes = [
        _video(collection, direct, f"Show - {number:02d}.mkv", number)
        for number in (1, 2)
    ]
    recap = _video(
        collection, direct, "Show - 05.mkv", 50, content_type_manual="recap",
    )
    session.add(collection)
    session.flush()
    set_video_episode_number_from_input(recap, "5.5")
    session.commit()
    return {
        "collection": collection.id,
        "title": direct.id,
        "recap": recap.id,
        "episodes": [video.id for video in episodes],
    }


def _temporary_app(tmp_path: Path, name: str):
    return create_app(Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / name}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))


def _orphaning_inference(title_name: str):
    """Simuluje pozdější automatic inference, která odpojí Season kontext."""
    original = hierarchy_evaluation.apply_automatic_structural_inference

    def patched(collection: CatalogCollection) -> bool:
        changed = original(collection)
        for title in collection.titles:
            if title.local_title != title_name:
                continue
            title.part_type_manual = None
            title.season_number_manual = None
            title.part_number_manual = None
            title.season_label_manual = None
            title.hierarchy_manual_override = False
            title.hierarchy_verified_at = None
            title.part_type = "title"
            title.season_number = None
            title.season_label = None
            changed = True
        return changed

    return patched


# --------------------------------------------------------------------------
# HIGH-1 – legacy porušení nesmí blokovat sdílenou finalizaci
# --------------------------------------------------------------------------

def test_legacy_recap_outside_season_does_not_block_unrelated_write():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        ids = _legacy_manual_split_collection(session)
        collection = session.get(CatalogCollection, ids["collection"])
        assert "recap_outside_season" in _issue_codes(collection)
        assert manual_split_titles(collection)

        classify_videos_in_place(
            session, collection.id, [ids["episodes"][0]], "bonus",
        )
        session.commit()

    with Session(engine) as session:
        collection = session.get(CatalogCollection, ids["collection"])
        assert session.get(Video, ids["episodes"][0]).content_type_manual == "bonus"
        # Legacy porušení zůstává viditelné jako derived review: nezmizelo,
        # ale ani nezablokovalo nesouvisející explicitní zápis.
        assert "recap_outside_season" in _issue_codes(collection)
        assert session.get(Video, ids["recap"]).recap_episode_number_manual_tenths == 55


def test_legacy_recap_outside_season_allows_corrective_write():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        ids = _legacy_manual_split_collection(session)
        collection = session.get(CatalogCollection, ids["collection"])
        assert "recap_outside_season" in _issue_codes(collection)

        set_manual_title_hierarchy(
            session.get(CatalogTitle, ids["title"]),
            season_number=1,
            season_label="S1",
            part_type="season",
            sort_order=None,
            hierarchy_verified=True,
        )
        session.commit()

    with Session(engine) as session:
        collection = session.get(CatalogCollection, ids["collection"])
        assert collection.titles[0].effective_part_type == "season"
        assert session.get(Video, ids["recap"]).catalog_title_id == ids["title"]
        assert "recap_outside_season" not in _issue_codes(collection)


def test_legacy_recap_outside_season_still_rejects_a_new_violation():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        ids = _legacy_manual_split_collection(session)
        collection = session.get(CatalogCollection, ids["collection"])
        before = _issue_codes(collection)

        with pytest.raises(ValueError, match="Season kontextu"):
            classify_videos_in_place(
                session, collection.id, [ids["episodes"][1]], "recap",
            )
        session.rollback()

    with Session(engine) as session:
        collection = session.get(CatalogCollection, ids["collection"])
        assert session.get(Video, ids["episodes"][1]).content_type_manual is None
        assert _issue_codes(collection) == before


def test_explicit_manual_split_creating_invalid_recap_stays_strict():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection = CatalogCollection(
            local_title="Show", normalized_local_title="show",
            relative_root_path="Anime/Show",
        )
        season = _season(collection)
        extras = _supplementary(
            collection, name="Extras", part_type="bonus",
            season_number=None, sort_order=1,
        )
        recap = _video(
            collection, season, "Show - 05.mkv", 50, content_type_manual="recap",
        )
        session.add(collection)
        session.flush()
        set_video_episode_number_from_input(recap, "5.5")
        session.commit()
        ids = {
            "collection": collection.id, "extras": extras.id,
            "season": season.id, "recap": recap.id,
        }
        assert "recap_outside_season" not in _issue_codes(collection)

        definition = ManualTitleDefinition(
            title_id=ids["extras"],
            local_title="Extras",
            manual_display_title=None,
            season_number_manual=None,
            season_label_manual=None,
            part_number_manual=None,
            part_type_manual="bonus",
            episode_start=None,
            episode_end=None,
            episode_start_offset=None,
            numbering_mode="unknown",
            sort_order=1,
            filename_pattern=None,
            video_ids=(ids["recap"],),
        )
        with pytest.raises(ValueError, match="Season kontextu"):
            apply_manual_split(session, ids["collection"], [definition])
        session.rollback()

    with Session(engine) as session:
        collection = session.get(CatalogCollection, ids["collection"])
        recap = session.get(Video, ids["recap"])
        assert recap.catalog_title_id == ids["season"]
        assert recap.manual_split_rule_videos == []
        assert "recap_outside_season" not in _issue_codes(collection)


# --------------------------------------------------------------------------
# HIGH-2 – explicitní finalizace pod strict guardem
# --------------------------------------------------------------------------

def _guarded_collection(session: Session):
    collection = CatalogCollection(
        local_title="Show", normalized_local_title="show",
        relative_root_path="Anime/Show",
    )
    season = _season(collection)
    recap_title = _supplementary(
        collection, name="Recap", part_type="recap",
        season_number=1, sort_order=9,
    )
    for number in (1, 2):
        _video(collection, season, f"Show - {number:02d}.mkv", number)
    recap = _video(
        collection, recap_title, "Recap 1.5.mkv", 30, file_type="recap",
    )
    session.add(collection)
    session.flush()
    refresh_collection_state(collection)
    session.commit()
    return collection, season, recap


def test_status_refresh_cannot_commit_a_new_recap_violation(tmp_path, monkeypatch):
    web_app = _temporary_app(tmp_path, "status-guard.db")
    with web_app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())
        collection, _season_title, recap = _guarded_collection(session)
        collection_id, recap_id = collection.id, recap.id
        assert _issue_codes(collection) == set()

    monkeypatch.setattr(
        hierarchy_evaluation,
        "apply_automatic_structural_inference",
        _orphaning_inference("Season 1"),
    )
    client = TestClient(web_app, raise_server_exceptions=False)
    response = client.post(
        f"/hierarchy-review/{collection_id}/status",
        data={"hierarchy_status": "automatic", "hierarchy_note": ""},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "Season kontextu" in response.json()["detail"]
    with web_app.state.sessions() as session:
        collection = session.get(CatalogCollection, collection_id)
        assert collection.titles
        assert session.get(Video, recap_id).catalog_title is not None
        assert {title.effective_part_type for title in collection.titles} == {
            "season", "recap",
        }


def test_confirm_part_is_protected_after_finalization(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection = CatalogCollection(
            local_title="Show", normalized_local_title="show",
            relative_root_path="Anime/Show",
        )
        title = CatalogTitle(
            collection=collection, local_title="Show",
            normalized_local_title="show",
            relative_root_path="Anime/Show",
            part_type="season", season_number=1, season_label="S1",
            numbering_mode="unknown",
        )
        for number in (1, 2):
            _video(collection, title, f"Show - {number:02d}.mkv", number)
        recap = _video(
            collection, title, "Recap 1.5.mkv", 30, file_type="recap",
        )
        session.add(collection)
        session.flush()
        session.commit()
        before = (
            title.part_type_manual,
            title.hierarchy_manual_override,
            title.hierarchy_verified_at,
        )

        monkeypatch.setattr(
            hierarchy_evaluation,
            "apply_automatic_structural_inference",
            _orphaning_inference("Show"),
        )
        with pytest.raises(ValueError, match="Season kontextu"):
            apply_single_title_confirmation(
                collection, part_type="season",
                season_number=1, season_label="S1",
            )

        session.rollback()
        title = session.get(CatalogTitle, title.id)
        assert (
            title.part_type_manual,
            title.hierarchy_manual_override,
            title.hierarchy_verified_at,
        ) == before
        assert session.get(Video, recap.id).catalog_title_id == title.id


def test_confirm_existing_split_season_parts_is_protected_after_finalization(
    monkeypatch,
):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection = CatalogCollection(
            local_title="Show", normalized_local_title="show",
            relative_root_path="Anime/Show",
        )
        first = _season(collection, name="Season 1 A", sort_order=0)
        second = _season(collection, name="Season 1 B", sort_order=1)
        recap_title = _supplementary(
            collection, name="Recap", part_type="recap",
            season_number=2, sort_order=9,
        )
        owner = _season(collection, name="Season 2", season_number=2, sort_order=2)
        for number in (1, 2):
            _video(collection, first, f"Show - {number:02d}.mkv", number)
        for number in (3, 4):
            _video(collection, second, f"Show - {number:02d}.mkv", number)
        _video(collection, owner, "Show S2 - 01.mkv", 21)
        recap = _video(
            collection, recap_title, "Recap 1.5.mkv", 30, file_type="recap",
        )
        session.add(collection)
        session.flush()
        refresh_collection_state(collection)
        session.commit()
        membership = {
            video.id: video.catalog_title_id for video in collection.videos
        }

        monkeypatch.setattr(
            hierarchy_evaluation,
            "apply_automatic_structural_inference",
            _orphaning_inference("Season 2"),
        )
        with pytest.raises(ValueError, match="Season kontextu"):
            confirm_existing_split_season_parts(
                session, collection.id, season_number=1,
                title_ids=[first.id, second.id],
            )

        session.rollback()
        collection = session.get(CatalogCollection, collection.id)
        assert {
            video.id: video.catalog_title_id for video in collection.videos
        } == membership
        assert session.get(CatalogTitle, first.id).part_number_manual is None
        assert session.get(CatalogTitle, second.id).part_number_manual is None
        assert session.get(Video, recap.id).catalog_title_id == recap_title.id


def test_metadata_split_is_protected_after_finalization(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection = CatalogCollection(
            local_title="Show", normalized_local_title="show",
            relative_root_path="Anime/Show",
        )
        ova = _supplementary(
            collection, name="OVA", part_type="ova",
            season_number=None, sort_order=1,
        )
        season = _season(collection, sort_order=0)
        recap_title = _supplementary(
            collection, name="Recap", part_type="recap",
            season_number=1, sort_order=9,
        )
        for number in (1, 2, 3):
            _video(collection, ova, f"OVA {number:02d}.mkv", 100 + number)
        _video(collection, season, "Show - 01.mkv", 1)
        _video(collection, season, "Show - 02.mkv", 2)
        recap = _video(
            collection, recap_title, "Recap 1.5.mkv", 30, file_type="recap",
        )
        ova.metadata_status = "linked_manual"
        ova.preferred_metadata_provider = "anilist"
        ova.preferred_external_id = "1"
        ova.metadata_record = TitleMetadata(
            display_title="OVA", episode_count=2,
            metadata_provider="anilist", metadata_external_id="1",
        )
        ova.external_links.append(ExternalTitleLink(
            provider="anilist", external_id="1", match_method="manual_search",
            is_primary=True, is_manual=True, verified_at=utc_now(),
        ))
        session.add(collection)
        session.flush()
        refresh_collection_state(collection)
        session.commit()
        titles_before = {title.id for title in collection.titles}
        membership = {
            video.id: video.catalog_title_id for video in collection.videos
        }

        monkeypatch.setattr(
            hierarchy_evaluation,
            "apply_automatic_structural_inference",
            _orphaning_inference("Season 1"),
        )
        with pytest.raises(ValueError, match="Season kontextu"):
            apply_metadata_split(session, ova.id, confirmed=True)

        session.rollback()
        collection = session.get(CatalogCollection, collection.id)
        # Žádný partial split: nová část ani přesun metadat nepřežily.
        assert {title.id for title in collection.titles} == titles_before
        assert {
            video.id: video.catalog_title_id for video in collection.videos
        } == membership
        assert session.get(CatalogTitle, ova.id).metadata_status == "linked_manual"
        assert session.get(Video, recap.id).catalog_title_id == recap_title.id


# --------------------------------------------------------------------------
# MEDIUM-2 – rozbitý řetězec Video -> Collection
# --------------------------------------------------------------------------

def test_missing_video_collection_keeps_the_owning_collection_affected():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        source = CatalogCollection(
            local_title="Show", normalized_local_title="show",
            relative_root_path="Anime/Show",
        )
        direct = CatalogTitle(
            collection=source, local_title="Show", normalized_local_title="show",
            relative_root_path="Anime/Show", part_type="season",
            season_number=1, season_label="S1", numbering_mode="unknown",
        )
        recap_title = _supplementary(
            source, name="Recap", part_type="recap",
            season_number=1, sort_order=9,
        )
        first = _video(source, direct, "Show - 01.mkv", 1)
        _video(source, direct, "Show - 02.mkv", 2)
        _video(source, recap_title, "Recap 1.5.mkv", 30, file_type="recap")

        target = CatalogCollection(
            local_title="Other", normalized_local_title="other",
            relative_root_path="Anime/Other",
        )
        other_season = _season(target, name="Other S1")
        session.add_all([source, target])
        session.flush()
        refresh_collection_state(source)
        refresh_collection_state(target)
        session.commit()
        assert _issue_codes(source) == set()

        # Podporovaný nekonzistentní stav: redundantní vazba chybí.
        source_id, direct_id = source.id, direct.id
        first_id, target_title_id = first.id, other_season.id
        first.catalog_collection = None
        session.flush()
        session.commit()

        with pytest.raises(ValueError, match="Season kontextu"):
            assign_known_videos_to_title(session, [first_id], target_title_id)
        session.rollback()

    with Session(engine) as session:
        source = session.get(CatalogCollection, source_id)
        stored = session.get(Video, first_id)
        assert stored.catalog_title_id == direct_id
        assert session.get(CatalogTitle, direct_id).effective_part_type == "season"
        assert _issue_codes(source) == set()


def test_missing_video_collection_move_finalizes_the_owning_collection():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        source = CatalogCollection(
            local_title="Show", normalized_local_title="show",
            relative_root_path="Anime/Show",
        )
        direct = CatalogTitle(
            collection=source, local_title="Show", normalized_local_title="show",
            relative_root_path="Anime/Show", part_type="season",
            season_number=1, season_label="S1", numbering_mode="unknown",
        )
        first = _video(source, direct, "Show - 01.mkv", 1)
        _video(source, direct, "Show - 02.mkv", 2)
        target = CatalogCollection(
            local_title="Other", normalized_local_title="other",
            relative_root_path="Anime/Other",
        )
        other_season = _season(target, name="Other S1")
        session.add_all([source, target])
        session.flush()
        refresh_collection_state(source)
        refresh_collection_state(target)
        session.commit()
        assert direct.effective_part_type == "season"

        first.catalog_collection = None
        session.flush()
        session.commit()

        direct_id, first_id, target_title_id = direct.id, first.id, other_season.id
        assign_known_videos_to_title(session, [first_id], target_title_id)
        session.commit()

    with Session(engine) as session:
        stored = session.get(CatalogTitle, direct_id)
        # Zdrojová collection byla skutečně finalizována, cache není stale.
        assert stored.effective_part_type == "title"
        assert stored.effective_season_number is None
        assert session.get(Video, first_id).catalog_title_id == target_title_id


# --------------------------------------------------------------------------
# HIGH-3 – assignment není manual-split selector authority
# --------------------------------------------------------------------------

def _scan_library_root(root: Path, filenames: tuple[str, ...]) -> Path:
    folder = root / "Show"
    folder.mkdir(parents=True, exist_ok=True)
    for index, filename in enumerate(filenames, 1):
        (folder / filename).write_bytes(b"v" * index)
    return folder


def test_ordinary_assignment_does_not_create_manual_split_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.scanner.service.probe_video", lambda *_a, **_k: PROBE_RESULT,
    )
    folder = _scan_library_root(
        tmp_path, ("Show - 01.mkv", "Show - 02.mkv", "Show - 03.mkv"),
    )
    (folder / "Bonus talk.mkv").write_bytes(b"bonus")
    engine = create_engine(f"sqlite:///{tmp_path / 'assignment.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        scan_library(session, tmp_path)
    with Session(engine) as session:
        collection = session.scalar(select(CatalogCollection).where(
            CatalogCollection.relative_root_path == "Show"
        ))
        bonus = next(
            video for video in collection.videos if video.filename == "Bonus talk.mkv"
        )
        title = collection.titles[0]
        assign_known_videos_to_title(session, [bonus.id], title.id)
        session.commit()
        collection_id = collection.id

    with Session(engine) as session:
        collection = session.get(CatalogCollection, collection_id)
        # Explicitní rozhodnutí o tomto videu zůstává uloženo...
        assert {
            link.video.filename
            for title in collection.titles
            for link in title.manual_split_rule_videos
        } == {"Bonus talk.mkv"}
        # ...ale collection tím nepřešla do manual-split režimu, který by
        # pokrytí pravidlem vyžadoval po všech ostatních videích.
        assert manual_split_rule_titles(collection) == []
        assert _issue_codes(collection) == set()


def test_new_episodes_are_assigned_after_an_ordinary_assignment(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.scanner.service.probe_video", lambda *_a, **_k: PROBE_RESULT,
    )
    folder = _scan_library_root(
        tmp_path, ("Show - 01.mkv", "Show - 02.mkv", "Show - 03.mkv"),
    )
    (folder / "Bonus talk.mkv").write_bytes(b"bonus")
    engine = create_engine(f"sqlite:///{tmp_path / 'assignment-scan.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        scan_library(session, tmp_path)
    with Session(engine) as session:
        collection = session.scalar(select(CatalogCollection).where(
            CatalogCollection.relative_root_path == "Show"
        ))
        bonus = next(
            video for video in collection.videos if video.filename == "Bonus talk.mkv"
        )
        assign_known_videos_to_title(session, [bonus.id], collection.titles[0].id)
        session.commit()
        collection_id = collection.id

    (folder / "Show - 04.mkv").write_bytes(b"vvvv")
    (folder / "Show - 05.mkv").write_bytes(b"vvvvv")
    with Session(engine) as session:
        scan_library(session, tmp_path)

    with Session(engine) as session:
        collection = session.get(CatalogCollection, collection_id)
        assignments = {
            video.filename: video.catalog_title_id for video in collection.videos
        }
        assert all(title_id is not None for title_id in assignments.values())
        assert "manual_split_unmatched" not in _issue_codes(collection)
        assert collection.hierarchy_status != "review_required"


def test_genuine_manual_split_still_reports_unmatched(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.scanner.service.probe_video", lambda *_a, **_k: PROBE_RESULT,
    )
    folder = _scan_library_root(tmp_path, ("Show - 01.mkv", "Show - 02.mkv"))
    engine = create_engine(f"sqlite:///{tmp_path / 'genuine-split.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        scan_library(session, tmp_path)
    with Session(engine) as session:
        collection = session.scalar(select(CatalogCollection).where(
            CatalogCollection.relative_root_path == "Show"
        ))
        title = collection.titles[0]
        apply_manual_split(session, collection.id, [ManualTitleDefinition(
            title_id=title.id,
            local_title="Season 1",
            manual_display_title=None,
            season_number_manual=1,
            season_label_manual="S1",
            part_number_manual=None,
            part_type_manual="season",
            episode_start=1,
            episode_end=2,
            episode_start_offset=None,
            numbering_mode="season_local",
            sort_order=1,
            filename_pattern=None,
            video_ids=(),
        )])
        session.commit()
        collection_id = collection.id

    (folder / "Show - 09.mkv").write_bytes(b"outside")
    with Session(engine) as session:
        scan_library(session, tmp_path)

    with Session(engine) as session:
        collection = session.get(CatalogCollection, collection_id)
        outside = next(
            video for video in collection.videos if video.filename == "Show - 09.mkv"
        )
        assert outside.catalog_title_id is None
        assert "manual_split_unmatched" in _issue_codes(collection)


# --------------------------------------------------------------------------
# MEDIUM-1 – scanner posuzuje Recap proti odvozenému Season kontextu
# --------------------------------------------------------------------------

def _scan_and_report(engine, root: Path):
    with Session(engine) as session:
        scan_library(session, root)
    with Session(engine) as session:
        collection = session.scalar(select(CatalogCollection).where(
            CatalogCollection.relative_root_path == "Show"
        ))
        recap = next(
            video for video in collection.videos if "Recap" in video.filename
        )
        return (
            collection.titles[0].effective_part_type,
            collection.titles[0].effective_season_number,
            recap.catalog_title_id,
            collection.hierarchy_status,
            _issue_codes(collection),
        )


def test_direct_root_recap_is_assigned_to_the_inferred_season(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.scanner.service.probe_video", lambda *_a, **_k: PROBE_RESULT,
    )
    _scan_library_root(
        tmp_path, ("Show - 01.mkv", "Show - 02.mkv", "Recap 3.5.mkv"),
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'direct-recap.db'}")
    Base.metadata.create_all(engine)

    part_type, season_number, recap_title_id, status, codes = _scan_and_report(
        engine, tmp_path,
    )
    assert (part_type, season_number) == ("season", 1)
    assert recap_title_id is not None
    assert status == "automatic"
    assert codes == set()


def test_direct_root_recap_scan_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.scanner.service.probe_video", lambda *_a, **_k: PROBE_RESULT,
    )
    _scan_library_root(
        tmp_path, ("Show - 01.mkv", "Show - 02.mkv", "Recap 3.5.mkv"),
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'direct-recap-idempotent.db'}")
    Base.metadata.create_all(engine)

    snapshots = [_scan_and_report(engine, tmp_path) for _ in range(3)]
    assert snapshots.count(snapshots[0]) == 3
    assert snapshots[0][2] is not None
    assert snapshots[0][4] == set()


def test_explicit_season_folder_recap_stays_correct(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.scanner.service.probe_video", lambda *_a, **_k: PROBE_RESULT,
    )
    folder = tmp_path / "Show" / "Season 1"
    folder.mkdir(parents=True)
    for index, filename in enumerate(
        ("Show - 01.mkv", "Show - 02.mkv", "Recap 3.5.mkv"), 1,
    ):
        (folder / filename).write_bytes(b"v" * index)
    engine = create_engine(f"sqlite:///{tmp_path / 'season-folder.db'}")
    Base.metadata.create_all(engine)

    part_type, season_number, recap_title_id, status, codes = _scan_and_report(
        engine, tmp_path,
    )
    assert (part_type, season_number) == ("season", 1)
    assert recap_title_id is not None
    assert status == "automatic"
    assert codes == set()


def test_direct_root_recap_and_later_episodes_stay_automatic(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.scanner.service.probe_video", lambda *_a, **_k: PROBE_RESULT,
    )
    folder = _scan_library_root(
        tmp_path, ("Show - 01.mkv", "Show - 02.mkv", "Recap 3.5.mkv"),
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'integration.db'}")
    Base.metadata.create_all(engine)

    _part_type, _season, recap_title_id, status, codes = _scan_and_report(
        engine, tmp_path,
    )
    assert recap_title_id is not None and status == "automatic" and codes == set()

    (folder / "Show - 03.mkv").write_bytes(b"vvv")
    (folder / "Show - 04.mkv").write_bytes(b"vvvv")
    with Session(engine) as session:
        scan_library(session, tmp_path)

    with Session(engine) as session:
        collection = session.scalar(select(CatalogCollection).where(
            CatalogCollection.relative_root_path == "Show"
        ))
        assert all(
            video.catalog_title_id is not None for video in collection.videos
        )
        assert "manual_split_unmatched" not in _issue_codes(collection)
        assert _issue_codes(collection) == set()
