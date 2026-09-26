"""Read-only supplementary audit tool follows the shared Hierarchy Review read model."""

import hashlib

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, selectinload

from app.database import Base
from app.hierarchy_review import (
    confirm_unnumbered_supplementary_copies,
    preview_unnumbered_supplementary_copies,
)
from app.models import CatalogCollection, CatalogTitle, Video
from app.supplementary import ORDINAL_TYPES, collection_supplementary_review
from app.tools.audit_supplementary import audit


def add_video(session, collection, title, filename, file_type, **kwargs):
    video = Video(
        catalog_title=title, catalog_collection=collection,
        relative_path=f"{collection.relative_root_path}/{filename}",
        root_folder=collection.relative_root_path, filename=filename,
        size=1, mtime_ns=1, file_type=file_type, **kwargs,
    )
    session.add(video)
    return video


def audit_db(tmp_path):
    database = tmp_path / "audit.db"
    engine = create_engine(f"sqlite:///{database}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        films = CatalogCollection(local_title="Tenki no Ko", normalized_local_title="tenki no ko",
                                  relative_root_path="Tenki no Ko")
        film = CatalogTitle(collection=films, local_title="Tenki no Ko",
                            normalized_local_title="tenki no ko",
                            relative_root_path="Tenki no Ko", part_type="film")
        copies = [add_video(session, films, film, name, "film")
                  for name in ("Tenki no Ko.mkv", "TenkiNoKo.m4v")]
        show = CatalogCollection(local_title="Show", normalized_local_title="show",
                                 relative_root_path="Show")
        # The primary has no videos; only the loaded collection supplies it.
        CatalogTitle(collection=show, local_title="Show S1", normalized_local_title="show s1",
                     relative_root_path="Show/S1", part_type="season", season_number=1)
        extras = [
            CatalogTitle(collection=show, local_title=f"Show {name}",
                         normalized_local_title=f"show {name.casefold()}",
                         relative_root_path=f"Show/{name}", part_type="bonus", season_number=1)
            for name in ("Extras", "Bonus")
        ]
        for index, title in enumerate(extras):
            add_video(session, show, title, f"Show - Extra material {'AB'[index]}.mkv",
                      "other", content_type_manual="special")
        session.commit()
        film_ids = [video.id for video in copies]
        preview = preview_unnumbered_supplementary_copies(session, films.id, film_ids, film_ids[0])
        confirm_unnumbered_supplementary_copies(
            session, films.id, film_ids, film_ids[0], preview.fingerprint,
        )
        session.commit()
    engine.dispose()
    return database, film_ids


def broken_relations_db(tmp_path):
    database = tmp_path / "broken.db"
    engine = create_engine(f"sqlite:///{database}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        legacy = CatalogCollection(local_title="Legacy", normalized_local_title="legacy",
                                   relative_root_path="Legacy")
        season = CatalogTitle(collection=legacy, local_title="Legacy", normalized_local_title="legacy",
                              relative_root_path="Legacy", part_type="season", season_number=1)
        primary = add_video(session, legacy, season, "Legacy - 01.mkv", "episode")
        # Legacy relation whose primary now sits in another typed group.
        retyped = add_video(session, legacy, season, "Legacy - 01 copy.mkv", "episode",
                            content_type_manual="ova")
        colliding = [add_video(session, legacy, season, f"Legacy - Opening {suffix}.mkv", "other",
                               content_type_manual="ncop", episode_number_manual_override=1)
                     for suffix in ("TV", "BD")]
        films = CatalogCollection(local_title="Kimi no Na wa", normalized_local_title="kimi no na wa",
                                  relative_root_path="Kimi no Na wa")
        film = CatalogTitle(collection=films, local_title="Kimi no Na wa",
                            normalized_local_title="kimi no na wa",
                            relative_root_path="Kimi no Na wa", part_type="film")
        copies = [add_video(session, films, film, name, "film")
                  for name in ("Kimi no Na wa.mkv", "Your Name.mkv")]
        session.flush()
        retyped.duplicate_of_video_id = primary.id
        session.commit()
        film_ids = [video.id for video in copies]
        preview = preview_unnumbered_supplementary_copies(session, films.id, film_ids, film_ids[0])
        confirm_unnumbered_supplementary_copies(
            session, films.id, film_ids, film_ids[0], preview.fingerprint,
        )
        # A later reclassification invalidates the confirmed same-content copy.
        copies[1].content_type_manual = "ova"
        session.commit()
        ids = dict(retyped=retyped.id, colliding=[video.id for video in colliding],
                   invalid_copy=copies[1].id)
    engine.dispose()
    return database, ids


def audited_pairs(result):
    return (
        {("missing_supplementary_ordinal", row["id"])
         for item in result["collision_candidates"] for row in item["unknown"]}
        | {("supplementary_ordinal_collision", video_id)
           for item in result["ordinal_collisions"] for video_id in item["video_ids"]}
        | {("broken_supplementary_identity", video_id)
           for item in result["broken_identities"] for video_id in item["video_ids"]}
    )


def shared_review_pairs(database):
    """Hierarchy Review issues, loaded per collection like the application does."""
    engine = create_engine(f"sqlite:///{database}")
    pairs = set()
    with Session(engine) as session:
        collections = session.scalars(select(CatalogCollection).options(
            selectinload(CatalogCollection.titles).selectinload(CatalogTitle.videos)
            .selectinload(Video.duplicate_of),
        )).all()
        for collection in collections:
            videos = [video for title in collection.titles for video in title.videos]
            for issues in collection_supplementary_review(videos, list(collection.titles)).values():
                pairs.update((issue.code, video.id) for issue in issues for video in issue.videos)
    engine.dispose()
    return pairs


def test_audit_counts_every_ordinal_type_and_follows_collection_scoped_review(tmp_path):
    database, film_ids = audit_db(tmp_path)
    fingerprint = hashlib.sha256(database.read_bytes()).hexdigest()
    stat = database.stat()

    result = audit(database)

    assert result["database_unchanged_during_audit"] is True
    assert hashlib.sha256(database.read_bytes()).hexdigest() == fingerprint
    assert (database.stat().st_size, database.stat().st_mtime_ns) == (stat.st_size, stat.st_mtime_ns)
    # Every shared ordinal type has a bucket; PV keeps its spelling bucket.
    assert set(result["totals"]) == ORDINAL_TYPES | {"pv"}
    assert result["totals"]["film"]["physical"] == 2
    assert result["totals"]["film"]["unknown"] == 2
    assert result["totals"]["special"]["physical"] == 2

    by_id = {row["id"]: row for row in result["rows"]}
    assert by_id[film_ids[1]]["duplicate_of"] == film_ids[0]
    # Two physical copies confirmed as one unnumbered film are one logical
    # identity, so they are not a missing-ordinal candidate.
    assert result["ordinal_collisions"] == []
    assert result["broken_identities"] == []
    [candidate] = result["collision_candidates"]
    assert candidate["type"] == "special"
    assert candidate["context"] == "Show S1"
    assert len(candidate["title_ids"]) == 2
    assert not {row["id"] for row in candidate["unknown"]} & set(film_ids)
    assert audited_pairs(result) == shared_review_pairs(database)


def test_audit_loads_cross_group_primaries_and_reports_collisions_and_broken_identities(tmp_path):
    database, ids = broken_relations_db(tmp_path)
    fingerprint = hashlib.sha256(database.read_bytes()).hexdigest()

    result = audit(database)

    assert result["database_unchanged_during_audit"] is True
    assert hashlib.sha256(database.read_bytes()).hexdigest() == fingerprint
    assert {row["id"] for row in result["rows"]} >= {ids["retyped"], ids["invalid_copy"]}
    [collision] = result["ordinal_collisions"]
    assert (collision["type"], collision["ordinal"]) == ("ncop", 1)
    assert sorted(collision["video_ids"]) == sorted(ids["colliding"])
    assert ids["invalid_copy"] in {
        video_id for item in result["broken_identities"] for video_id in item["video_ids"]
    }
    assert audited_pairs(result) == shared_review_pairs(database)
