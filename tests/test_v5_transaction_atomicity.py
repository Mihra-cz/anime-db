"""Regrese pro V5-CLOSURE-B1: jedna skutečná atomická hranice semantic write.

Audit prokázal, že ``strict_hierarchy_write_guard`` otevíral SAVEPOINT v době,
kdy pysqlite ještě žádnou transakci nezačal.  SQLite pak transakci založil až
kvůli samotnému SAVEPOINTu, takže jeho ``RELEASE`` provedl skutečný commit a
následný ``session.rollback()`` už neměl co vrátit.  Výsledkem byl partial
semantic write: route vrátila HTTP 400, ale část změny fyzicky přežila.

Testy proto vždy ověřují stav z NOVÉ session/connection, ne z ORM identity map,
a používají ``app.database.make_engine`` – tedy přesně ten engine setup, který
používá aplikace.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import Base, make_engine, make_session_factory
from app.hierarchy_evaluation import (
    RecapSeasonContextError,
    strict_hierarchy_write_guard,
)
from app.hierarchy_review import (
    move_titles_to_collection,
    record_manual_collection_merge,
    refresh_collection_state,
)
from app.main import create_app
from app.models import (
    CatalogCollection,
    CatalogTitle,
    CollectionGroupingDecision,
    Video,
)
from app.numbering import set_video_episode_number_from_input
from app.scanner import scan_library


PROBE_RESULT = {
    "duration": 60.0, "video_codec": "h264", "width": 1920, "height": 1080,
    "audio": [], "subtitles": [],
}


class _SimulatedWorkflowFailure(RuntimeError):
    """Chyba kroku workflow, který následuje až po úspěšném guardu."""


def _engine(tmp_path: Path, name: str = "atomicity.db"):
    engine = make_engine(f"sqlite:///{tmp_path / name}")
    Base.metadata.create_all(engine)
    return engine


def _library(tmp_path: Path, monkeypatch, names=("Main", "Fragment")) -> Path:
    for name in names:
        for episode in (1, 2):
            path = tmp_path / "Anime" / name / f"E{episode:02}.mkv"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"video")
    monkeypatch.setattr(
        "app.scanner.service.probe_video", lambda _path, **_kwargs: PROBE_RESULT,
    )
    return tmp_path


def _single_collection(session: Session) -> CatalogCollection:
    collection = CatalogCollection(
        local_title="Show", normalized_local_title="show",
        relative_root_path="Anime/Show",
    )
    title = CatalogTitle(
        collection=collection, local_title="Season 1",
        normalized_local_title="season 1",
        relative_root_path="Anime/Show/Season 1",
        part_type="season", season_number=1, season_label="S1",
        numbering_mode="unknown",
    )
    for episode in (1, 2):
        session.add(Video(
            relative_path=f"{title.relative_root_path}/Show - {episode:02d}.mkv",
            root_folder="Anime", filename=f"Show - {episode:02d}.mkv",
            size=episode, mtime_ns=episode, file_type="episode",
            catalog_collection=collection, catalog_title=title,
        ))
    session.add(collection)
    session.flush()
    refresh_collection_state(collection)
    session.commit()
    return collection


# ---------------------------------------------------------------------------
# Transakční invariant samotného engine setupu
# ---------------------------------------------------------------------------

def test_savepoint_release_never_commits_outside_the_owning_transaction(tmp_path):
    """RELEASE SAVEPOINT nesmí být skutečný commit ani bez předchozího DML."""
    engine = _engine(tmp_path)
    sessions = make_session_factory(engine)
    with sessions() as session:
        session.add(CatalogCollection(
            local_title="Original", normalized_local_title="original",
            relative_root_path="Anime/Original",
        ))
        session.commit()

    with sessions() as session:
        # Pouze SELECT před savepointem – přesně jako move_titles_to_collection.
        collection = session.scalar(select(CatalogCollection))
        savepoint = session.begin_nested()
        collection.local_title = "MUTATED"
        session.flush()
        savepoint.commit()
        session.rollback()

    with sessions() as verify:
        assert verify.scalar(select(CatalogCollection)).local_title == "Original"
    engine.dispose()


def test_sqlite_transaction_is_owned_by_sqlalchemy_not_by_the_driver(tmp_path):
    engine = _engine(tmp_path)
    sessions = make_session_factory(engine)
    with sessions() as session:
        session.execute(select(CatalogCollection))
        raw = session.connection().connection.dbapi_connection
        assert raw.isolation_level is None
        assert raw.in_transaction is True
        session.begin_nested().commit()
        assert raw.in_transaction is True
    engine.dispose()


# ---------------------------------------------------------------------------
# B1-A – isolated guard
# ---------------------------------------------------------------------------

def test_b1_a_guard_success_then_later_failure_rolls_everything_back(tmp_path):
    engine = _engine(tmp_path)
    sessions = make_session_factory(engine)
    with sessions() as session:
        collection = _single_collection(session)
        video_id = collection.videos[0].id
        collection_id = collection.id

    with sessions() as session:
        collection = session.get(CatalogCollection, collection_id)
        video = session.get(Video, video_id)
        before = video.episode_number_manual_override
        with strict_hierarchy_write_guard(session, [collection]):
            set_video_episode_number_from_input(video, "3")
            refresh_collection_state(collection)
        assert video.episode_number_manual_override == 3
        # Krok workflow, který selže až po úspěšném guardu.
        with pytest.raises(_SimulatedWorkflowFailure):
            raise _SimulatedWorkflowFailure("krok po guardu")
        session.rollback()

    with sessions() as verify:
        assert verify.get(Video, video_id).episode_number_manual_override == before
    engine.dispose()


def test_b1_a_session_close_without_commit_also_rolls_the_guard_back(tmp_path):
    """Route se spoléhá na ``with sessions()``; close() musí stačit."""
    engine = _engine(tmp_path)
    sessions = make_session_factory(engine)
    with sessions() as session:
        collection = _single_collection(session)
        video_id = collection.videos[0].id
        collection_id = collection.id

    with pytest.raises(_SimulatedWorkflowFailure):
        with sessions() as session:
            collection = session.get(CatalogCollection, collection_id)
            video = session.get(Video, video_id)
            with strict_hierarchy_write_guard(session, [collection]):
                set_video_episode_number_from_input(video, "3")
                refresh_collection_state(collection)
            raise _SimulatedWorkflowFailure("krok po guardu")

    with sessions() as verify:
        assert verify.get(Video, video_id).episode_number_manual_override is None
    engine.dispose()


# ---------------------------------------------------------------------------
# B1-C – invalid write uvnitř guardu
# ---------------------------------------------------------------------------

def test_b1_c_new_invalid_recap_write_persists_nothing(tmp_path):
    engine = _engine(tmp_path)
    sessions = make_session_factory(engine)
    with sessions() as session:
        collection = CatalogCollection(
            local_title="Show", normalized_local_title="show",
            relative_root_path="Anime/Show",
        )
        direct = CatalogTitle(
            collection=collection, local_title="Show",
            normalized_local_title="show",
            relative_root_path="Anime/Show",
            part_type="title", numbering_mode="unknown",
        )
        episode = Video(
            relative_path="Anime/Show/Show - 01.mkv", root_folder="Anime",
            filename="Show - 01.mkv", size=1, mtime_ns=1, file_type="episode",
            catalog_collection=collection, catalog_title=direct,
        )
        candidate = Video(
            relative_path="Anime/Show/Show - 02.mkv", root_folder="Anime",
            filename="Show - 02.mkv", size=2, mtime_ns=2, file_type="episode",
            catalog_collection=collection, catalog_title=direct,
        )
        session.add(collection)
        session.flush()
        refresh_collection_state(collection)
        session.commit()
        collection_id, candidate_id = collection.id, candidate.id
        assert episode.id is not None

    with sessions() as session:
        collection = session.get(CatalogCollection, collection_id)
        candidate = session.get(Video, candidate_id)
        with pytest.raises(RecapSeasonContextError):
            with strict_hierarchy_write_guard(session, [collection]):
                candidate.content_type_manual = "recap"
                set_video_episode_number_from_input(candidate, "1.5")
                refresh_collection_state(collection)
        session.commit()

    with sessions() as verify:
        candidate = verify.get(Video, candidate_id)
        assert candidate.content_type_manual is None
        assert candidate.recap_episode_number_manual_tenths is None
    engine.dispose()


# ---------------------------------------------------------------------------
# B1-B / B1-D / B1-E – collection move workflow
# ---------------------------------------------------------------------------

def _scanned_move_fixture(tmp_path: Path, monkeypatch) -> dict:
    _library(tmp_path, monkeypatch)
    engine = _engine(tmp_path, "move.db")
    sessions = make_session_factory(engine)
    with sessions() as session:
        scan_library(session, tmp_path)
        target = session.scalar(select(CatalogCollection).where(
            CatalogCollection.relative_root_path == "Anime/Main"
        ))
        source = session.scalar(select(CatalogCollection).where(
            CatalogCollection.relative_root_path == "Anime/Fragment"
        ))
        moved = source.titles[0]
        session.commit()
        state = {
            "engine": engine,
            "sessions": sessions,
            "source_id": source.id,
            "target_id": target.id,
            "moved_id": moved.id,
        }
    return state


def _move_snapshot(session: Session, state: dict) -> dict:
    moved = session.get(CatalogTitle, state["moved_id"])
    source = session.get(CatalogCollection, state["source_id"])
    target = session.get(CatalogCollection, state["target_id"])
    return {
        "title_collection": moved.catalog_collection_id,
        "structural": (
            moved.part_type, moved.season_number, moved.part_number,
            moved.season_label,
        ),
        "video_membership": tuple(sorted(
            (video.id, video.catalog_collection_id, video.catalog_title_id)
            for video in moved.videos
        )),
        "source_titles": tuple(sorted(title.id for title in source.titles)),
        "target_titles": tuple(sorted(title.id for title in target.titles)),
        "source_status": (source.hierarchy_status, source.hierarchy_note),
        "target_status": (target.hierarchy_status, target.hierarchy_note),
        "grouping_authority": tuple(sorted(
            (item.suggestion_key, item.decision, item.target_collection_path)
            for item in session.scalars(select(CollectionGroupingDecision)).all()
        )),
    }


def test_b1_b_collection_move_service_rolls_back_when_a_later_step_fails(
    tmp_path, monkeypatch,
):
    state = _scanned_move_fixture(tmp_path, monkeypatch)
    sessions = state["sessions"]
    with sessions() as session:
        before = _move_snapshot(session, state)

    with pytest.raises(_SimulatedWorkflowFailure):
        with sessions() as session:
            move_titles_to_collection(
                session, state["target_id"], [state["moved_id"]],
            )
            # Selhání až po úspěšné hierarchy validaci, před uložením
            # grouping authority – přesně auditní reprodukce A.
            raise _SimulatedWorkflowFailure("record_manual_collection_merge")

    with sessions() as verify:
        assert _move_snapshot(verify, state) == before
        assert verify.get(
            CatalogTitle, state["moved_id"],
        ).catalog_collection_id == state["source_id"]
    state["engine"].dispose()


def test_b1_b_collection_move_route_returns_400_without_partial_write(
    tmp_path, monkeypatch,
):
    state = _scanned_move_fixture(tmp_path, monkeypatch)
    sessions = state["sessions"]
    with sessions() as session:
        before = _move_snapshot(session, state)

    def failing_merge(*_args, **_kwargs):
        raise ValueError("grouping authority nelze uložit")

    monkeypatch.setattr("app.main.record_manual_collection_merge", failing_merge)
    app = create_app(Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'move.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))
    with TestClient(app) as client:
        response = client.post(
            "/hierarchy-review/collections/move",
            data={
                "title_ids": [str(state["moved_id"])],
                "target_collection_id": str(state["target_id"]),
            },
            follow_redirects=False,
        )
    assert response.status_code == 400

    with sessions() as verify:
        assert _move_snapshot(verify, state) == before
    state["engine"].dispose()


def test_b1_d_successful_collection_move_commits_every_related_change(
    tmp_path, monkeypatch,
):
    state = _scanned_move_fixture(tmp_path, monkeypatch)
    sessions = state["sessions"]
    app = create_app(Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'move.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))
    with TestClient(app) as client:
        response = client.post(
            "/hierarchy-review/collections/move",
            data={
                "title_ids": [str(state["moved_id"])],
                "target_collection_id": str(state["target_id"]),
            },
            follow_redirects=False,
        )
    assert response.status_code == 303

    with sessions() as verify:
        snapshot = _move_snapshot(verify, state)
        assert snapshot["title_collection"] == state["target_id"]
        assert snapshot["source_titles"] == ()
        assert state["moved_id"] in snapshot["target_titles"]
        assert len(snapshot["grouping_authority"]) == 1
        decision = snapshot["grouping_authority"][0]
        assert decision[1] == "merged"
        assert decision[2] == "Anime/Main"
        assert all(
            collection_id == state["target_id"]
            for _video_id, collection_id, _title_id in snapshot["video_membership"]
        )
    state["engine"].dispose()


def test_b1_e_cross_collection_rollback_leaves_no_hybrid_state(
    tmp_path, monkeypatch,
):
    state = _scanned_move_fixture(tmp_path, monkeypatch)
    sessions = state["sessions"]
    with sessions() as session:
        before = _move_snapshot(session, state)
        assert before["structural"] == ("season", 1, None, "S1")

    with pytest.raises(ValueError):
        with sessions() as session:
            target = move_titles_to_collection(
                session, state["target_id"], [state["moved_id"]],
            )
            # Guard prošel a hierarchy je finalizovaná; teprve teď selže
            # persistence grouping authority.
            record_manual_collection_merge(
                session, target, [state["moved_id"]],
            )
            raise ValueError("workflow selhalo před commitem")

    with sessions() as verify:
        after = _move_snapshot(verify, state)
    assert after == before
    assert after["grouping_authority"] == ()
    assert after["source_titles"] == (state["moved_id"],)
    assert after["structural"] == ("season", 1, None, "S1")
    state["engine"].dispose()
