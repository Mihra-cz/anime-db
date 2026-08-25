from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import Request

from app.config import Settings
from app.database import Base
from app.hierarchy_review import (
    set_manual_title_hierarchy,
    supplementary_assignment_recommendations,
    supplementary_video_suggestions,
)
from app.main import create_app
from app.metadata.split import apply_metadata_split, evaluate_metadata_split
from app.migrations import migrate_schema
from app.models import (
    Artwork,
    CatalogCollection,
    CatalogTitle,
    ExternalTitleLink,
    ManualSplitRuleVideo,
    MetadataCandidate,
    TitleMetadata,
    Video,
    utc_now,
)
from app.scanner import scan_library


PROBE_RESULT = {
    "duration": 60.0,
    "video_codec": "h264",
    "width": 1920,
    "height": 1080,
    "audio": [],
    "subtitles": [],
}


def web_request(web_app, path: str) -> Request:
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


def supplementary_title(
    filenames: list[str],
    *,
    part_type: str = "special",
    verified: bool = True,
) -> tuple[CatalogCollection, CatalogTitle, list[Video]]:
    collection = CatalogCollection(
        local_title="High School DxD",
        normalized_local_title="high school dxd",
        relative_root_path="Anime/High School DxD",
        hierarchy_status="verified" if verified else "review_required",
        hierarchy_verified_at=utc_now() if verified else None,
    )
    title = CatalogTitle(
        collection=collection,
        local_title=(
            "NC – High School DxD Born"
            if part_type == "bonus"
            else "Specials – High School DxD Born"
        ),
        normalized_local_title="supplementary born",
        relative_root_path=f"Anime/High School DxD/{part_type}",
        part_type=part_type,
        season_number=3,
        season_label="S3",
        hierarchy_manual_override=verified,
        part_type_manual=part_type if verified else None,
        season_number_manual=3 if verified else None,
        season_label_manual="S3" if verified else None,
        sort_order_manual=2 if verified else None,
        hierarchy_verified_at=utc_now() if verified else None,
    )
    videos = [
        Video(
            relative_path=f"{title.relative_root_path}/{filename}",
            root_folder="Anime",
            filename=filename,
            size=1,
            mtime_ns=index,
            file_type=(
                "special" if filename.casefold().startswith("special")
                else "op" if filename.casefold().startswith("op")
                else "ed" if filename.casefold().startswith("ed")
                else "other"
            ),
            catalog_title=title,
            catalog_collection=collection,
        )
        for index, filename in enumerate(filenames, 1)
    ]
    return collection, title, videos


def attach_confirmed_metadata(
    title: CatalogTitle,
    episode_count: int,
    *,
    external_id: str = "12345",
    display_title: str = "High School DxD Born Specials A",
) -> None:
    title.metadata_status = "linked_manual"
    title.preferred_metadata_provider = "anilist"
    title.preferred_external_id = external_id
    title.metadata_record = TitleMetadata(
        display_title=display_title,
        title_romaji=display_title,
        episode_count=episode_count,
        metadata_provider="anilist",
        metadata_external_id=external_id,
    )
    title.external_links.append(ExternalTitleLink(
        provider="anilist",
        external_id=external_id,
        match_method="manual_search",
        is_primary=True,
        is_manual=True,
        verified_at=utc_now(),
    ))


def test_verified_special_and_bonus_do_not_reopen_generic_hierarchy_split():
    _, special, special_videos = supplementary_title([
        f"Special {number:02}.mkv" for number in range(1, 7)
    ])
    _, bonus, bonus_videos = supplementary_title(
        ["ED.mkv", "OP 01.mkv", "OP 02.mkv"], part_type="bonus",
    )

    assert supplementary_video_suggestions(
        special_videos,
        include_video_ids={video.id for video in special_videos},
    ) == ()
    assert supplementary_assignment_recommendations(special_videos) == ()
    assert supplementary_video_suggestions(
        bonus_videos,
        include_video_ids={video.id for video in bonus_videos},
    ) == ()
    assert supplementary_assignment_recommendations(bonus_videos) == ()
    assert special.effective_part_type == "special"
    assert bonus.effective_part_type == "bonus"


def test_unverified_supplementary_context_can_keep_real_hierarchy_review():
    _, _, videos = supplementary_title(
        ["High School DxD OP 01.mkv"], part_type="bonus", verified=False,
    )

    suggestions = supplementary_video_suggestions(videos)

    assert len(suggestions) == 1
    assert suggestions[0].video is videos[0]
    assert suggestions[0].supplementary_type == "op"


def test_verified_but_mismatched_supplementary_type_keeps_hierarchy_conflict_action():
    _, _, videos = supplementary_title(
        ["OVA 01.mkv"], part_type="bonus", verified=True,
    )

    suggestions = supplementary_video_suggestions(videos)

    assert len(suggestions) == 1
    assert suggestions[0].supplementary_type == "ova"
    assert suggestions[0].proposed_part_type == "ova"


def test_metadata_split_requires_confirmed_metadata_and_skips_full_coverage():
    _, title, _ = supplementary_title([
        f"Special {number:02}.mkv" for number in range(1, 7)
    ])

    assert evaluate_metadata_split(title) is None

    attach_confirmed_metadata(title, 6)

    assert evaluate_metadata_split(title) is None


def test_confirmed_metadata_exact_subset_is_read_only_recommendation():
    _, title, videos = supplementary_title([
        f"Special {number:02}.mkv" for number in range(1, 7)
    ])
    attach_confirmed_metadata(title, 3)
    before = [
        (
            video.catalog_title,
            video.catalog_title_id,
            video.relative_path,
            video.file_type,
            video.content_type_manual,
        )
        for video in videos
    ]

    evaluation = evaluate_metadata_split(title)

    assert evaluation is not None and evaluation.is_recommendation
    assert [video.filename for video in evaluation.matching_videos] == [
        "Special 01.mkv", "Special 02.mkv", "Special 03.mkv",
    ]
    assert [video.filename for video in evaluation.remaining_videos] == [
        "Special 04.mkv", "Special 05.mkv", "Special 06.mkv",
    ]
    assert evaluation.part_type == "special"
    assert evaluation.season_context == "S3"
    assert evaluation.proposed_local_title == "Special – S3"
    assert [
        (
            video.catalog_title,
            video.catalog_title_id,
            video.relative_path,
            video.file_type,
            video.content_type_manual,
        )
        for video in videos
    ] == before


@pytest.mark.parametrize(
    ("part_type", "filenames"),
    [
        ("ova", ["OVA 01.mkv", "OVA 02.mkv", "OVA 03.mkv"]),
        ("special", ["Special 01.mkv", "Special 02.mkv", "Special 03.mkv"]),
        ("bonus", ["Bonus 01.mkv", "Bonus 02.mkv", "Bonus 03.mkv"]),
        ("film", ["Movie 01.mkv", "Movie 02.mkv", "Movie 03.mkv"]),
    ],
)
def test_metadata_split_numbering_semantics_cover_supplementary_and_film_parts(
    part_type,
    filenames,
):
    _, title, _ = supplementary_title(filenames, part_type=part_type)
    attach_confirmed_metadata(title, 2)

    evaluation = evaluate_metadata_split(title)

    assert evaluation is not None and evaluation.is_recommendation
    assert evaluation.part_type == part_type
    assert [video.filename for video in evaluation.matching_videos] == filenames[:2]
    assert [video.filename for video in evaluation.remaining_videos] == filenames[2:]


@pytest.mark.parametrize(
    "filenames",
    [
        ["Special 01.mkv", "Special 03.mkv", "Special 04.mkv"],
        ["Special.mkv", "Special 01.mkv", "Special 02.mkv"],
        ["OP 01.mkv", "ED 02.mkv", "ED 03.mkv"],
    ],
)
def test_metadata_subset_without_one_safe_numbering_match_is_ambiguity(filenames):
    _, title, _ = supplementary_title(filenames)
    attach_confirmed_metadata(title, 1)

    evaluation = evaluate_metadata_split(title)

    assert evaluation is not None and evaluation.is_ambiguous
    assert evaluation.matching_videos == ()
    assert "bezpeč" in evaluation.reason or "jednoznač" in evaluation.reason


def test_explicit_metadata_split_moves_metadata_and_preserves_hierarchy_and_content():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection, source, videos = supplementary_title([
            f"Special {number:02}.mkv" for number in range(1, 7)
        ])
        attach_confirmed_metadata(source, 3)
        source.metadata_locked = True
        source.metadata_candidates.extend([
            MetadataCandidate(
                provider="anilist",
                external_id="12345",
                candidate_title="Specials A",
                confirmed_at=utc_now(),
            ),
            MetadataCandidate(
                provider="anilist",
                external_id="67890",
                candidate_title="Specials B",
            ),
        ])
        source.artwork.append(Artwork(
            provider="anilist",
            external_id="12345",
            artwork_type="cover",
            remote_url="https://img.example/12345.jpg",
            local_path="anilist/12345/cover.jpg",
            mime_type="image/jpeg",
            file_size=100,
            is_primary=True,
        ))
        session.add(collection)
        session.commit()
        source_id = source.id
        original_paths = {video.filename: video.relative_path for video in videos}
        original_types = {video.filename: video.file_type for video in videos}

        with pytest.raises(ValueError, match="nutné potvrdit"):
            apply_metadata_split(session, source_id, confirmed=False)
        assert session.query(CatalogTitle).count() == 1

        result = apply_metadata_split(
            session,
            source_id,
            confirmed=True,
            local_title="Special – High School DxD Born metadata A",
        )
        new_id = result.new_title.id
        session.commit()

    with Session(engine) as session:
        source = session.get(CatalogTitle, source_id)
        new_title = session.get(CatalogTitle, new_id)
        assert [video.filename for video in new_title.videos] == [
            "Special 01.mkv", "Special 02.mkv", "Special 03.mkv",
        ]
        assert [video.filename for video in source.videos] == [
            "Special 04.mkv", "Special 05.mkv", "Special 06.mkv",
        ]
        assert new_title.effective_part_type == source.effective_part_type == "special"
        assert new_title.effective_season_number == source.effective_season_number == 3
        assert new_title.effective_season_label == source.effective_season_label == "S3"
        assert new_title.local_title == "Special – High School DxD Born metadata A"
        assert source.local_title == "Specials – High School DxD Born"
        assert new_title.sort_order_manual is None
        assert new_title.hierarchy_manual_override is True
        assert new_title.hierarchy_verified_at is not None
        assert new_title.metadata_locked is True
        assert source.metadata_locked is False
        assert source.metadata_record is None
        assert source.metadata_status == "candidates_available"
        assert new_title.metadata_record.metadata_external_id == "12345"
        assert [link.external_id for link in new_title.external_links if link.is_primary] == [
            "12345"
        ]
        assert [candidate.external_id for candidate in new_title.metadata_candidates] == [
            "12345"
        ]
        assert [candidate.external_id for candidate in source.metadata_candidates] == [
            "67890"
        ]
        assert [artwork.external_id for artwork in new_title.artwork] == ["12345"]
        assert source.artwork == []
        assert {
            link.video.filename for link in new_title.manual_split_rule_videos
        } == {"Special 01.mkv", "Special 02.mkv", "Special 03.mkv"}
        for video in [*new_title.videos, *source.videos]:
            assert video.relative_path == original_paths[video.filename]
            assert video.file_type == original_types[video.filename]
            assert video.content_type_manual is None


def test_metadata_split_does_not_override_existing_range_selector_authority():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection, source, _ = supplementary_title([
            f"Special {number:02}.mkv" for number in range(1, 7)
        ])
        source.episode_filename_pattern = r"^Special"
        attach_confirmed_metadata(source, 3)
        session.add(collection)
        session.commit()
        source_id = source.id

        with pytest.raises(ValueError, match="manual-split authority"):
            apply_metadata_split(session, source_id, confirmed=True)
        session.rollback()

        assert session.query(CatalogTitle).count() == 1
        assert len(session.get(CatalogTitle, source_id).videos) == 6
        assert session.get(CatalogTitle, source_id).metadata_record is not None


def test_metadata_split_survives_startup_sync_and_normal_rescan_without_filesystem_write(
    tmp_path: Path,
    monkeypatch,
):
    folder = tmp_path / "High School DxD" / "Specials"
    folder.mkdir(parents=True)
    paths = [folder / f"Special {number:02}.mkv" for number in range(1, 7)]
    for path in paths:
        path.write_bytes(f"video-{path.stem}".encode())
    before_bytes = {path: path.read_bytes() for path in paths}
    before_mtime = {path: path.stat().st_mtime_ns for path in paths}
    monkeypatch.setattr("app.scanner.service.probe_video", lambda _, **__: PROBE_RESULT)
    engine = create_engine(f"sqlite:///{tmp_path / 'metadata-split.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)

    with sessions() as session:
        scan_library(session, tmp_path)
        source = session.scalar(select(CatalogTitle).where(
            CatalogTitle.local_title == "Specials"
        ))
        set_manual_title_hierarchy(
            source,
            part_type="special",
            season_number=3,
            season_label="S3",
            sort_order=None,
            hierarchy_verified=True,
        )
        attach_confirmed_metadata(source, 3)
        session.commit()
        source_id = source.id
        result = apply_metadata_split(session, source_id, confirmed=True)
        new_id = result.new_title.id
        session.commit()

    migrate_schema(engine)
    with sessions() as session:
        scan_library(session, tmp_path)
        source = session.get(CatalogTitle, source_id)
        new_title = session.get(CatalogTitle, new_id)
        assert [video.filename for video in new_title.videos] == [
            "Special 01.mkv", "Special 02.mkv", "Special 03.mkv",
        ]
        assert [video.filename for video in source.videos] == [
            "Special 04.mkv", "Special 05.mkv", "Special 06.mkv",
        ]
        assert new_title.metadata_record.metadata_external_id == "12345"
        assert source.metadata_record is None
        assert new_title.effective_part_type == "special"
        assert new_title.effective_season_number == 3
        assert new_title.local_title == "Special – S3"
        assert new_title.sort_order_manual is None
        assert source.sort_order_manual is None
        assert len(session.scalars(select(ManualSplitRuleVideo)).all()) == 3

    assert {path: path.read_bytes() for path in paths} == before_bytes
    assert {path: path.stat().st_mtime_ns for path in paths} == before_mtime


def test_metadata_check_renders_safe_split_and_hierarchy_review_keeps_all_title_cards(
    tmp_path: Path,
):
    web_app = create_app(Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'metadata-split-web.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))
    with web_app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())
        collection, source, videos = supplementary_title([
            f"Special {number:02}.mkv" for number in range(1, 7)
        ])
        season = CatalogTitle(
            collection=collection,
            local_title="Season 3",
            normalized_local_title="season 3",
            relative_root_path="Anime/High School DxD/Season 3",
            part_type="season",
            season_number=3,
            season_label="S3",
        )
        attach_confirmed_metadata(source, 3)
        session.add(collection)
        session.commit()
        collection_id, source_id = collection.id, source.id
        before_assignments = {video.id: video.catalog_title_id for video in videos}

    endpoints = {
        route.path: route.endpoint for route in web_app.routes
        if hasattr(route, "endpoint")
    }
    detail = endpoints["/titles/{catalog_title_id}"](
        web_request(web_app, f"/titles/{source_id}"), source_id,
    ).body.decode()
    metadata_review = endpoints["/metadata-review"](
        web_request(web_app, "/metadata-review"), status="split",
    ).body.decode()
    hierarchy_review = endpoints["/hierarchy-review/{collection_id}"](
        web_request(web_app, f"/hierarchy-review/{collection_id}"), collection_id,
    ).body.decode()

    assert "Potvrzená metadata pokrývají pouze část lokální skupiny" in detail
    assert "High School DxD Born Specials A" in detail
    assert "Special 01.mkv" in detail and "Special 03.mkv" in detail
    assert "Special 04.mkv" in detail and "Special 06.mkv" in detail
    assert "typ části <strong>Special</strong>" in detail
    assert "season context <strong>S3</strong>" in detail
    assert "Rozdělit podle potvrzených metadat" in detail
    assert 'name="local_title" placeholder="Special – S3"' in detail
    assert 'name="confirm_split" value="true" required' in detail
    assert "Rozdělení podle metadat" in metadata_review
    assert "Přesunout 3 · ponechat 3" in metadata_review
    assert hierarchy_review.count('class="panel hierarchy-title-card') == 2
    assert f'id="title-{source_id}"' in hierarchy_review
    assert "Season 3" in hierarchy_review
    assert "Pravděpodobně doplňkový obsah" not in hierarchy_review

    with web_app.state.sessions() as session:
        assert {
            video.id: video.catalog_title_id
            for video in session.scalars(select(Video)).all()
            if video.id in before_assignments
        } == before_assignments


def test_metadata_split_route_is_post_only_and_requires_explicit_confirmation(tmp_path):
    web_app = create_app(Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'metadata-split-route.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))
    with web_app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())
        collection, source, _ = supplementary_title([
            f"Special {number:02}.mkv" for number in range(1, 7)
        ])
        attach_confirmed_metadata(source, 3)
        session.add(collection)
        session.commit()
        source_id = source.id

    route = next(
        route for route in web_app.routes
        if getattr(route, "path", None)
        == "/catalog/{filter_name}/titles/{catalog_title_id}/metadata/split"
    )
    assert route.methods == {"POST"}

    response = route.endpoint(
        "all", source_id, confirm_split=False, local_title="", q="", sort="", direction="",
        detail_sort="", detail_direction="",
    )

    assert response.status_code == 303
    assert "metadata_error=" in response.headers["location"]
    with web_app.state.sessions() as session:
        assert session.query(CatalogTitle).count() == 1
        assert session.get(CatalogTitle, source_id).metadata_record is not None
