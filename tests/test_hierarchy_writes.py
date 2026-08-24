import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.config import Settings
from app.hierarchy_evaluation import evaluate_collection_hierarchy
from app.main import create_app
from app.models import (
    CatalogCollection,
    CatalogTitle,
    ManualSplitRuleVideo,
    Video,
    utc_now,
)
from app.database import Base


def _app(tmp_path, database_name: str):
    app = create_app(Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / database_name}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))
    with app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())
    return app


def _endpoint(app, path: str):
    return next(
        route.endpoint for route in app.routes
        if getattr(route, "path", None) == path
    )


def _video(collection, title, filename="E01.mkv"):
    return Video(
        relative_path=f"{collection.relative_root_path}/{filename}",
        root_folder="Anime",
        filename=filename,
        size=1,
        mtime_ns=1,
        catalog_collection=collection,
        catalog_title=title,
    )


def test_status_route_confirms_complete_snapshot_then_uses_evaluator(tmp_path):
    app = _app(tmp_path, "status-confirm.db")
    with app.state.sessions() as session:
        collection = CatalogCollection(
            local_title="Show", normalized_local_title="show",
            relative_root_path="Anime/Show",
        )
        title = CatalogTitle(
            collection=collection, local_title="Season 1",
            normalized_local_title="season 1", relative_root_path="Anime/Show",
            part_type="season", season_number=1, season_label="S1",
        )
        session.add(_video(collection, title))
        session.commit()
        collection_id, title_id = collection.id, title.id

    response = _endpoint(app, "/hierarchy-review/{collection_id}/status")(
        collection_id,
        hierarchy_status="verified",
        hierarchy_note="Tento text nesmí být autorita.",
    )
    assert response.status_code == 303

    with app.state.sessions() as session:
        collection = session.get(CatalogCollection, collection_id)
        title = session.get(CatalogTitle, title_id)
        result = evaluate_collection_hierarchy(collection, list(collection.videos))
        assert title.hierarchy_manual_override is True
        assert title.part_type_manual == "season"
        assert title.season_number_manual == 1
        assert title.hierarchy_verified_at is not None
        assert collection.hierarchy_status == result.status == "verified"
        assert collection.hierarchy_note == result.primary_note is None


def test_status_route_cannot_force_or_overwrite_real_conflict(tmp_path):
    app = _app(tmp_path, "status-conflict.db")
    with app.state.sessions() as session:
        collection = CatalogCollection(
            local_title="Show", normalized_local_title="show",
            relative_root_path="Anime/Show",
        )
        first = CatalogTitle(
            collection=collection, local_title="Season 1",
            normalized_local_title="season 1",
            relative_root_path="Anime/Show/.catalog-part-1",
            part_type="season", season_number=1,
        )
        second = CatalogTitle(
            collection=collection, local_title="Season 2",
            normalized_local_title="season 2",
            relative_root_path="Anime/Show/.catalog-part-2",
            part_type="season", season_number=2,
        )
        video = _video(collection, None)
        session.add_all([
            collection,
            video,
            ManualSplitRuleVideo(catalog_title=first, video=video),
            ManualSplitRuleVideo(catalog_title=second, video=video),
        ])
        session.commit()
        collection_id = collection.id

    endpoint = _endpoint(app, "/hierarchy-review/{collection_id}/status")
    assert endpoint(
        collection_id, hierarchy_status="verified", hierarchy_note="",
    ).status_code == 303
    with app.state.sessions() as session:
        collection = session.get(CatalogCollection, collection_id)
        assert collection.hierarchy_status == "conflict"
        conflict_note = collection.hierarchy_note
        assert conflict_note

    assert endpoint(
        collection_id, hierarchy_status="automatic", hierarchy_note="fake",
    ).status_code == 303
    with app.state.sessions() as session:
        collection = session.get(CatalogCollection, collection_id)
        result = evaluate_collection_hierarchy(collection, list(collection.videos))
        assert collection.hierarchy_status == result.status == "conflict"
        assert collection.hierarchy_note == result.primary_note == conflict_note


def test_confirmation_validation_failure_leaves_no_partial_authority(tmp_path):
    app = _app(tmp_path, "status-atomic.db")
    with app.state.sessions() as session:
        collection = CatalogCollection(
            local_title="Show", normalized_local_title="show",
            relative_root_path="Anime/Show",
        )
        first = CatalogTitle(
            collection=collection, local_title="Season 1",
            normalized_local_title="season 1",
            relative_root_path="Anime/Show/Season 1",
            part_type="season", season_number=1,
        )
        invalid = CatalogTitle(
            collection=collection, local_title="Part",
            normalized_local_title="part",
            relative_root_path="Anime/Show/Part",
            part_type="part", season_number=1, part_number=None,
        )
        session.add_all([collection, first, invalid])
        session.commit()
        collection_id, title_ids = collection.id, (first.id, invalid.id)

    with pytest.raises(HTTPException) as raised:
        _endpoint(app, "/hierarchy-review/{collection_id}/status")(
            collection_id, hierarchy_status="verified", hierarchy_note="",
        )
    assert raised.value.status_code == 400

    with app.state.sessions() as session:
        titles = [session.get(CatalogTitle, title_id) for title_id in title_ids]
        assert all(title.hierarchy_manual_override is False for title in titles)
        assert all(title.hierarchy_verified_at is None for title in titles)
        assert all(title.part_type_manual is None for title in titles)


def test_title_numbering_route_finalizes_collection_status_and_note(tmp_path):
    app = _app(tmp_path, "numbering-finalizer.db")
    with app.state.sessions() as session:
        collection = CatalogCollection(
            local_title="Show", normalized_local_title="show",
            relative_root_path="Anime/Show",
            hierarchy_status="conflict", hierarchy_note="stale result",
        )
        title = CatalogTitle(
            collection=collection, local_title="Season 1",
            normalized_local_title="season 1", relative_root_path="Anime/Show",
            part_type="season", season_number=1, season_label="S1",
            part_type_manual="season", season_number_manual=1,
            season_label_manual="S1", sort_order_manual=1,
            hierarchy_manual_override=True, hierarchy_verified_at=utc_now(),
        )
        session.add(_video(collection, title))
        session.commit()
        collection_id, title_id = collection.id, title.id

    response = _endpoint(app, "/titles/{catalog_title_id}/numbering")(
        title_id,
        numbering_mode="season_local",
        episode_start_offset="",
        filter_name="all",
        q="",
        sort="",
        direction="",
        detail_sort="",
        detail_direction="",
    )
    assert response.status_code == 303

    with app.state.sessions() as session:
        collection = session.get(CatalogCollection, collection_id)
        result = evaluate_collection_hierarchy(collection, list(collection.videos))
        assert collection.hierarchy_status == result.status == "verified"
        assert collection.hierarchy_note == result.primary_note is None
