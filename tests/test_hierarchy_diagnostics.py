import re

import pytest

from app.config import Settings
from app.database import Base
from app.hierarchy_review import (
    GENERIC_TITLE_REVIEW_REASON,
    PROBABLE_GROUPING_REVIEW_REASON,
    catalog_title_hierarchy_is_verified,
    hierarchy_review_diagnostics,
)
from app.main import create_app
from app.models import CatalogCollection, CatalogTitle, Video, utc_now
from starlette.requests import Request


def _collection(*, status="review_required", note=None):
    return CatalogCollection(
        id=1,
        local_title="Test Anime",
        normalized_local_title="test anime",
        relative_root_path="Anime/Test Anime",
        hierarchy_status=status,
        hierarchy_note=note,
    )


def _title(
    collection, title_id, local_title, *, part_type="season", season_number=1,
    relative_root_path=None, **values,
):
    return CatalogTitle(
        id=title_id,
        collection=collection,
        local_title=local_title,
        normalized_local_title=local_title.casefold(),
        relative_root_path=(
            relative_root_path
            if relative_root_path is not None
            else f"{collection.relative_root_path}/{local_title}"
        ),
        part_type=part_type,
        season_number=season_number,
        season_label=f"S{season_number}" if season_number is not None else None,
        **values,
    )


def _video(
    collection, video_id, filename, *, title=None, episode_number=None, **values,
):
    return Video(
        id=video_id,
        relative_path=(
            f"{title.relative_root_path if title else collection.relative_root_path}/"
            f"{filename}"
        ),
        root_folder="Anime",
        filename=filename,
        size=1,
        mtime_ns=video_id,
        season_episode_number=episode_number,
        local_episode_number=episode_number,
        absolute_episode_number=episode_number,
        catalog_title=title,
        catalog_title_id=title.id if title is not None else None,
        catalog_collection=collection,
        catalog_collection_id=collection.id,
        **values,
    )


def _episodes(collection, title, count, *, first_id=1):
    return [
        _video(
            collection,
            first_id + number - 1,
            f"Episode {number:02}.mkv",
            title=title,
            episode_number=number,
        )
        for number in range(1, count + 1)
    ]


def _web_request(web_app, path):
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


def _detail_endpoint(web_app):
    return next(
        route.endpoint
        for route in web_app.routes
        if getattr(route, "path", None) == "/hierarchy-review/{collection_id}"
    )


def test_only_the_catalog_title_that_causes_review_is_marked():
    collection = _collection()
    healthy = _title(collection, 1, "Season 1")
    ambiguous = _title(
        collection, 2, "Unclear", part_type="title", season_number=None,
    )
    videos = [
        _video(collection, 1, "Episode 01.mkv", title=healthy, episode_number=1),
        _video(collection, 2, "Unclear 01.mkv", title=ambiguous, episode_number=1),
    ]

    diagnostics = hierarchy_review_diagnostics(collection, videos)

    assert diagnostics.title_has_blocking_issue(ambiguous)
    assert not diagnostics.title_has_blocking_issue(healthy)
    assert diagnostics.for_title(healthy) == ()
    assert diagnostics.affected_title_ids == (ambiguous.id,)
    assert diagnostics.affected_title_count == 1


def test_filename_season_conflict_is_localized_to_its_video():
    collection = _collection()
    title = _title(collection, 1, "Season 1")
    healthy = _video(
        collection, 1, "S01E01.mkv", title=title, episode_number=1,
    )
    conflicting = _video(
        collection, 2, "S02E02.mkv", title=title, episode_number=2,
    )

    diagnostics = hierarchy_review_diagnostics(collection, [healthy, conflicting])

    assert diagnostics.for_video(healthy) == ()
    assert diagnostics.video_has_blocking_issue(conflicting)
    issues = diagnostics.for_video(conflicting)
    assert len(issues) == 1
    assert issues[0].scope == "video"
    assert "filename" in issues[0].message.casefold()
    assert diagnostics.for_title_card(title) == issues


def test_unnumbered_supplementary_is_localized_to_its_video():
    collection = _collection()
    title = _title(collection, 1, "Season 1")
    video = _video(
        collection, 1, "S01E14 [SP]-The Common Cold.mkv", title=title,
    )

    diagnostics = hierarchy_review_diagnostics(collection, [video])

    issues = diagnostics.for_video(video)
    assert [issue.code for issue in issues] == ["supplementary_without_number"]
    assert issues[0].blocking


def test_numbering_gap_and_unknown_are_localized_to_title_and_video():
    collection = _collection()
    title = _title(collection, 1, "Season 1")
    first = _video(
        collection, 1, "Episode 01.mkv", title=title, episode_number=1,
    )
    third = _video(
        collection, 2, "Episode 03.mkv", title=title, episode_number=3,
    )
    unknown = _video(collection, 3, "Mystery.mkv", title=title)

    diagnostics = hierarchy_review_diagnostics(collection, [first, third, unknown])

    assert any(
        issue.code == "numbering_gap" and "E2" in issue.message
        for issue in diagnostics.for_title(title)
    )
    assert [issue.code for issue in diagnostics.for_video(unknown)] == [
        "unknown_or_missing_numbering"
    ]
    assert diagnostics.for_video(first) == ()
    assert diagnostics.for_video(third) == ()


def test_unresolved_and_confirmed_duplicate_groups_stay_video_scoped():
    unresolved_collection = _collection()
    unresolved_title = _title(unresolved_collection, 1, "Season 1")
    unresolved = [
        _video(
            unresolved_collection, identifier, f"Copy {identifier}.mkv",
            title=unresolved_title, episode_number=1,
        )
        for identifier in (1, 2)
    ]

    unresolved_diagnostics = hierarchy_review_diagnostics(
        unresolved_collection, unresolved,
    )

    unresolved_issue = next(
        issue for issue in unresolved_diagnostics.issues
        if issue.code == "canonical_duplicate"
    )
    assert unresolved_issue.scope == "video"
    assert unresolved_issue.videos == tuple(unresolved)

    confirmed_collection = _collection()
    confirmed_title = _title(confirmed_collection, 1, "Season 1")
    primary = _video(
        confirmed_collection, 1, "Primary.mkv",
        title=confirmed_title, episode_number=1,
    )
    duplicate = _video(
        confirmed_collection, 2, "Duplicate.mkv",
        title=confirmed_title, episode_number=1,
        duplicate_of=primary, duplicate_of_video_id=primary.id,
    )

    confirmed_diagnostics = hierarchy_review_diagnostics(
        confirmed_collection, [primary, duplicate],
    )

    confirmed_issue = next(
        issue for issue in confirmed_diagnostics.issues
        if issue.code == "confirmed_duplicate"
    )
    assert confirmed_issue.scope == "video"
    assert set(confirmed_issue.videos) == {primary, duplicate}
    assert not any(
        issue.code == "canonical_duplicate"
        for issue in confirmed_diagnostics.issues
    )


def test_missing_duplicate_primary_is_localized_to_secondary_video():
    collection = _collection()
    title = _title(collection, 1, "Season 1")
    video = _video(
        collection, 1, "Orphaned duplicate.mkv", title=title,
        episode_number=1, duplicate_primary_missing=True,
    )

    diagnostics = hierarchy_review_diagnostics(collection, [video])

    assert any(
        issue.code == "duplicate_primary_missing"
        for issue in diagnostics.for_video(video)
    )


def test_unknown_persisted_reason_remains_a_collection_fallback():
    collection = _collection(
        status="conflict",
        note="Konflikt ručních pravidel zasahuje více částí.",
    )
    title = _title(collection, 1, "Season 1")
    videos = [_video(
        collection, 1, "Episode 01.mkv", title=title, episode_number=1,
    )]

    diagnostics = hierarchy_review_diagnostics(collection, videos)

    assert len(diagnostics.collection_issues) == 1
    issue = diagnostics.collection_issues[0]
    assert issue.scope == "collection"
    assert issue.message == collection.hierarchy_note
    assert diagnostics.for_title(title) == ()
    assert diagnostics.for_video(videos[0]) == ()


def test_stale_known_summary_remains_visible_as_collection_fallback():
    collection = _collection(note=GENERIC_TITLE_REVIEW_REASON)
    title = _title(collection, 1, "Season 1")
    video = _video(
        collection, 1, "Episode 01.mkv", title=title, episode_number=1,
    )

    diagnostics = hierarchy_review_diagnostics(collection, [video])

    assert diagnostics.blocking_count == 1
    assert diagnostics.collection_issues[0].code == (
        "legacy_unlocalized_review_state"
    )
    assert diagnostics.collection_issues[0].message == GENERIC_TITLE_REVIEW_REASON


def test_multiple_reasons_are_kept_in_their_own_scopes():
    collection = _collection()
    ambiguous = _title(
        collection, 1, "Unclear", part_type="title", season_number=None,
    )
    season = _title(collection, 2, "Season 1")
    ambiguous_video = _video(
        collection, 1, "Unclear 01.mkv", title=ambiguous, episode_number=1,
    )
    conflicting_video = _video(
        collection, 2, "S02E01.mkv", title=season, episode_number=1,
    )

    diagnostics = hierarchy_review_diagnostics(
        collection, [ambiguous_video, conflicting_video],
    )

    assert diagnostics.title_has_blocking_issue(ambiguous)
    assert diagnostics.video_has_blocking_issue(conflicting_video)
    assert not diagnostics.video_has_blocking_issue(ambiguous_video)
    assert diagnostics.blocking_count == 2
    assert diagnostics.affected_title_ids == (1, 2)
    assert diagnostics.affected_video_ids == (2,)


def test_named_child_context_is_recreated_as_structured_title_issue():
    collection = _collection(note=PROBABLE_GROUPING_REVIEW_REASON)
    collection.relative_root_path = "Anime/High School DxD (Z12-J18)"
    title = _title(
        collection,
        1,
        "High School DxD Born (J15)",
        part_type="title",
        season_number=None,
    )
    video = _video(
        collection, 1, "E01.mkv", title=title, episode_number=1,
    )

    diagnostics = hierarchy_review_diagnostics(collection, [video])

    assert [issue.code for issue in diagnostics.for_title(title)] == [
        "related_named_child",
        "generic_structural_type",
    ]
    assert diagnostics.for_video(video) == ()
    assert diagnostics.collection_issues == ()
    contextual = diagnostics.for_title(title)[0]
    assert contextual.scope == "catalog_title"
    assert contextual.message == PROBABLE_GROUPING_REVIEW_REASON


def test_persisted_manual_split_conflict_remains_explicit_legacy_fallback():
    collection = _collection(
        status="conflict", note="Konflikt překrývajících se pravidel.",
    )
    first = _title(
        collection,
        1,
        "Part 1",
        part_type="part",
        season_number=1,
        part_type_manual="part",
        season_number_manual=1,
        part_number_manual=1,
        hierarchy_manual_override=True,
        episode_start=1,
        episode_end=12,
        sort_order_manual=1,
    )
    _title(
        collection,
        2,
        "Part 2",
        part_type="part",
        season_number=1,
        part_type_manual="part",
        season_number_manual=1,
        part_number_manual=2,
        hierarchy_manual_override=True,
        episode_start=1,
        episode_end=12,
        sort_order_manual=2,
    )
    video = _video(
        collection, 1, "Episode 01.mkv", title=first, episode_number=1,
    )

    diagnostics = hierarchy_review_diagnostics(collection, [video])

    assert diagnostics.for_video(video) == ()
    assert len(diagnostics.collection_issues) == 1
    issue = diagnostics.collection_issues[0]
    assert issue.code == "legacy_unlocalized_review_state"
    assert issue.message == "Konflikt překrývajících se pravidel."
    assert diagnostics.evaluation.status == "conflict"


@pytest.mark.parametrize("verified", [False, True])
def test_automatic_and_verified_titles_have_no_false_diagnostics(verified):
    collection = _collection(status="verified" if verified else "automatic")
    title = _title(
        collection,
        1,
        "Season 1",
        hierarchy_manual_override=verified,
        hierarchy_verified_at=utc_now() if verified else None,
        part_type_manual="season" if verified else None,
        season_number_manual=1 if verified else None,
        season_label_manual="S1" if verified else None,
    )
    video = _video(
        collection, 1, "Episode 01.mkv", title=title, episode_number=1,
    )

    diagnostics = hierarchy_review_diagnostics(collection, [video])

    assert diagnostics.blocking_issues == ()
    assert diagnostics.for_title_card(title) == ()
    assert catalog_title_hierarchy_is_verified(title) is verified


def test_historical_incomplete_part_snapshot_is_a_local_title_issue():
    collection = _collection()
    timestamp = utc_now()
    title = _title(
        collection,
        1,
        "Part 2",
        part_type="migration_review",
        season_number=None,
        part_number=2,
        part_type_manual="part",
        season_number_manual=1,
        season_label_manual="Part 2",
        part_number_manual=None,
        hierarchy_manual_override=True,
        hierarchy_verified_at=timestamp,
    )
    video = _video(
        collection, 1, "Episode 01.mkv", title=title, episode_number=1,
    )

    diagnostics = hierarchy_review_diagnostics(collection, [video])

    assert not catalog_title_hierarchy_is_verified(title)
    assert diagnostics.title_has_blocking_issue(title)
    assert any(
        "číslo Part" in issue.message for issue in diagnostics.for_title(title)
    )
    assert title.part_number_manual is None
    assert title.hierarchy_verified_at == timestamp


@pytest.mark.parametrize("count", [15, 24])
def test_flat_15_to_24_episode_notice_is_nonblocking(count):
    collection = _collection(status="automatic")
    title = _title(
        collection,
        1,
        collection.local_title,
        relative_root_path=collection.relative_root_path,
    )
    videos = _episodes(collection, title, count)

    diagnostics = hierarchy_review_diagnostics(collection, videos)

    assert diagnostics.blocking_issues == ()
    notices = [issue for issue in diagnostics.for_title(title) if not issue.blocking]
    assert len(notices) == 1
    assert f"E1–E{count}" in notices[0].message


def test_flat_over_24_episode_reason_is_blocking_and_title_scoped():
    collection = _collection()
    title = _title(
        collection,
        1,
        collection.local_title,
        relative_root_path=collection.relative_root_path,
    )
    videos = _episodes(collection, title, 25)

    diagnostics = hierarchy_review_diagnostics(collection, videos)

    assert diagnostics.title_has_blocking_issue(title)
    assert diagnostics.collection_issues == ()
    issues = diagnostics.for_title(title)
    assert any(issue.blocking and "dlouhá" in issue.message for issue in issues)
    assert not any(not issue.blocking for issue in issues)


def test_endpoint_localizes_one_title_issue_and_keeps_other_card_neutral(tmp_path):
    web_app = create_app(Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'localized-title-issues.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))
    with web_app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())
        collection = CatalogCollection(
            local_title="Two Parts",
            normalized_local_title="two parts",
            relative_root_path="Anime/Two Parts",
            hierarchy_status="review_required",
        )
        healthy = CatalogTitle(
            collection=collection,
            local_title="Season 1",
            normalized_local_title="season 1",
            relative_root_path="Anime/Two Parts/Season 1",
            part_type="season",
            season_number=1,
            season_label="S1",
        )
        ambiguous = CatalogTitle(
            collection=collection,
            local_title="Mystery",
            normalized_local_title="mystery",
            relative_root_path="Anime/Two Parts/Mystery",
            part_type="title",
        )
        session.add(collection)
        session.flush()
        for video_id, title in enumerate((healthy, ambiguous), 1):
            session.add(Video(
                relative_path=f"{title.relative_root_path}/Episode 01.mkv",
                root_folder="Anime",
                filename="Episode 01.mkv",
                size=1,
                mtime_ns=video_id,
                local_episode_number=1,
                season_episode_number=1,
                absolute_episode_number=1,
                catalog_title=title,
                catalog_collection=collection,
            ))
        session.commit()
        collection_id = collection.id
        healthy_id = healthy.id
        ambiguous_id = ambiguous.id

    rendered = _detail_endpoint(web_app)(
        _web_request(web_app, f"/hierarchy-review/{collection_id}"), collection_id,
    ).body.decode()

    healthy_opening = re.search(
        rf'<article class="([^"]*)" id="title-{healthy_id}">', rendered,
    ).group(1)
    ambiguous_opening = re.search(
        rf'<article class="([^"]*)" id="title-{ambiguous_id}">', rendered,
    ).group(1)
    healthy_card = rendered.split(
        f'id="title-{healthy_id}"', 1,
    )[1].split("</article>", 1)[0]
    ambiguous_card = rendered.split(
        f'id="title-{ambiguous_id}"', 1,
    )[1].split("</article>", 1)[0]
    assert "has-blocking-issue" not in healthy_opening
    assert "has-blocking-issue" in ambiguous_opening
    assert "Typ části nelze bezpečně určit" not in healthy_card
    assert "Typ části nelze bezpečně určit" in ambiguous_card
    assert re.search(r'href="#title-issue-\d+-generic', rendered)
    assert "1 blokující problém" in rendered
    assert "1 dotčená část" in rendered


def test_endpoint_keeps_relational_conflict_in_collection_summary(tmp_path):
    web_app = create_app(Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'collection-level-issue.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))
    note = "Konflikt ručních pravidel zasahuje více částí."
    with web_app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())
        collection = CatalogCollection(
            local_title="Relational Conflict",
            normalized_local_title="relational conflict",
            relative_root_path="Anime/Relational Conflict",
            hierarchy_status="conflict",
            hierarchy_note=note,
        )
        title = CatalogTitle(
            collection=collection,
            local_title="Season 1",
            normalized_local_title="season 1",
            relative_root_path="Anime/Relational Conflict/Season 1",
            part_type="season",
            season_number=1,
            season_label="S1",
        )
        Video(
            relative_path=f"{title.relative_root_path}/Episode 01.mkv",
            root_folder="Anime",
            filename="Episode 01.mkv",
            size=1,
            mtime_ns=1,
            local_episode_number=1,
            season_episode_number=1,
            absolute_episode_number=1,
            catalog_title=title,
            catalog_collection=collection,
        )
        session.add(collection)
        session.commit()
        collection_id = collection.id
        title_id = title.id

    rendered = _detail_endpoint(web_app)(
        _web_request(web_app, f"/hierarchy-review/{collection_id}"), collection_id,
    ).body.decode()

    summary = rendered.split('id="hierarchy-collection-issues"', 1)[1].split(
        "</div>", 1,
    )[0]
    opening = re.search(
        rf'<article class="([^"]*)" id="title-{title_id}">', rendered,
    ).group(1)
    assert note in summary
    assert "Problémy celé collection" in summary
    assert "has-blocking-issue" not in opening
    assert "1 blokující problém" in rendered
    assert "dotčená část" not in rendered


def test_endpoint_keeps_nonstandard_resolution_at_the_problem_video(tmp_path):
    web_app = create_app(Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'localized-video-issue.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))
    with web_app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())
        collection = CatalogCollection(
            local_title="Fractional",
            normalized_local_title="fractional",
            relative_root_path="Anime/Fractional",
            hierarchy_status="review_required",
        )
        title = CatalogTitle(
            collection=collection,
            local_title=collection.local_title,
            normalized_local_title=collection.normalized_local_title,
            relative_root_path=collection.relative_root_path,
            part_type="season",
            season_number=1,
            season_label="S1",
        )
        video = Video(
            relative_path="Anime/Fractional/Fractional - 04.5.mkv",
            root_folder="Anime",
            filename="Fractional - 04.5.mkv",
            size=1,
            mtime_ns=1,
            catalog_title=title,
            catalog_collection=collection,
        )
        session.add(collection)
        session.commit()
        collection_id = collection.id
        title_id = title.id
        video_id = video.id

    rendered = _detail_endpoint(web_app)(
        _web_request(web_app, f"/hierarchy-review/{collection_id}"), collection_id,
    ).body.decode()
    card = rendered.split(f'id="title-{title_id}"', 1)[1].split(
        "</article>", 1,
    )[0]

    assert f'id="video-issue-{video_id}-nonstandard_numbering"' in card
    assert "Toto video je důvod kontroly" in card
    assert "Vyřešit jako" in card
    assert "Potvrdit řešení" in card
