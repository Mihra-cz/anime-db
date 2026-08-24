from dataclasses import replace

import pytest

from app.hierarchy_evaluation import (
    HierarchyIssueCode,
    HierarchyIssueScope,
    derive_hierarchy_status,
    evaluate_collection_hierarchy,
)
from app.hierarchy_authority import (
    ManualHierarchyAuthorityState,
    manual_hierarchy_authority_state,
    manual_hierarchy_snapshot_requires_preservation,
    manual_hierarchy_snapshot_uses_legacy_projection,
)
from app.hierarchy_review import (
    hierarchy_review_diagnostics,
    refresh_collection_state,
)
from app.models import CatalogCollection, CatalogTitle, Video, utc_now
from app.numbering import summarize_title_numbering


def _collection(*, status: str = "automatic", note: str | None = None):
    return CatalogCollection(
        id=1,
        local_title="Test Anime",
        normalized_local_title="test anime",
        relative_root_path="Anime/Test Anime",
        hierarchy_status=status,
        hierarchy_note=note,
    )


def _title(
    collection: CatalogCollection,
    *,
    title_id: int = 1,
    local_title: str | None = None,
    relative_root_path: str | None = None,
    part_type: str = "season",
    season_number: int | None = 1,
    part_number: int | None = None,
    **values,
) -> CatalogTitle:
    local_title = local_title or collection.local_title
    return CatalogTitle(
        id=title_id,
        collection=collection,
        local_title=local_title,
        normalized_local_title=local_title.casefold(),
        relative_root_path=(
            collection.relative_root_path
            if relative_root_path is None
            else relative_root_path
        ),
        part_type=part_type,
        season_number=season_number,
        part_number=part_number,
        season_label=f"S{season_number}" if season_number is not None else None,
        **values,
    )


def _video(
    collection: CatalogCollection,
    title: CatalogTitle | None,
    video_id: int,
    filename: str,
    *,
    episode_number: int | None = None,
    **values,
) -> Video:
    root = title.relative_root_path if title is not None else collection.relative_root_path
    return Video(
        id=video_id,
        relative_path=f"{root}/{filename}",
        root_folder="Anime",
        filename=filename,
        size=1,
        mtime_ns=video_id,
        local_episode_number=episode_number,
        season_episode_number=episode_number,
        absolute_episode_number=episode_number,
        catalog_title=title,
        catalog_title_id=title.id if title is not None else None,
        catalog_collection=collection,
        catalog_collection_id=collection.id,
        **values,
    )


def _episodes(collection: CatalogCollection, title: CatalogTitle, count: int):
    return [
        _video(
            collection,
            title,
            number,
            f"Episode {number:02}.mkv",
            episode_number=number,
        )
        for number in range(1, count + 1)
    ]


def _codes(result):
    return {issue.code for issue in result.issues}


@pytest.mark.parametrize("count", [12, 14])
def test_direct_root_short_series_is_automatic_without_blocking_issue(count):
    collection = _collection(status="review_required")
    title = _title(collection, part_type="title", season_number=None)
    _episodes(collection, title, count)

    refresh_collection_state(collection)
    result = evaluate_collection_hierarchy(collection, list(collection.videos))

    assert title.effective_part_type == "season"
    assert title.effective_season_number == 1
    assert result.status == "automatic"
    assert result.blocking_issues == ()
    assert HierarchyIssueCode.SOFT_LONG_FLAT_SERIES not in _codes(result)


@pytest.mark.parametrize("count", [15, 24])
def test_direct_root_medium_series_has_only_soft_warning(count):
    collection = _collection(status="review_required")
    title = _title(collection, part_type="title", season_number=None)
    _episodes(collection, title, count)

    refresh_collection_state(collection)
    result = evaluate_collection_hierarchy(collection, list(collection.videos))

    assert collection.hierarchy_status == "automatic"
    assert result.status == "automatic"
    assert result.blocking_issues == ()
    warnings = [
        issue for issue in result.issues
        if issue.code == HierarchyIssueCode.SOFT_LONG_FLAT_SERIES
    ]
    assert len(warnings) == 1
    assert not warnings[0].blocking
    assert warnings[0].catalog_title is title
    assert f"E1–E{count}" in warnings[0].message


def test_direct_root_long_series_has_title_scoped_blocking_issue():
    collection = _collection()
    title = _title(collection, part_type="title", season_number=None)
    _episodes(collection, title, 25)

    refresh_collection_state(collection)
    result = evaluate_collection_hierarchy(collection, list(collection.videos))

    issue = next(
        item for item in result.issues
        if item.code == HierarchyIssueCode.LONG_FLAT_SERIES
    )
    assert result.status == "review_required"
    assert issue.blocking
    assert issue.scope == HierarchyIssueScope.CATALOG_TITLE
    assert issue.catalog_title is title
    assert "E1–E25" in issue.message


def test_explicit_season_two_is_not_subject_to_flat_length_gate():
    collection = _collection()
    title = _title(
        collection,
        local_title="Season 2",
        relative_root_path="Anime/Test Anime/Season 2",
        season_number=2,
    )
    _episodes(collection, title, 25)

    refresh_collection_state(collection)
    result = evaluate_collection_hierarchy(collection, list(collection.videos))

    assert result.status == "automatic"
    assert HierarchyIssueCode.LONG_FLAT_SERIES not in _codes(result)
    assert HierarchyIssueCode.SOFT_LONG_FLAT_SERIES not in _codes(result)


def test_gap_has_stable_title_scoped_code():
    collection = _collection()
    title = _title(collection)
    _video(collection, title, 1, "Episode 01.mkv", episode_number=1)
    _video(collection, title, 2, "Episode 03.mkv", episode_number=3)

    result = evaluate_collection_hierarchy(collection, list(collection.videos))

    issue = next(item for item in result.issues if item.code == HierarchyIssueCode.NUMBERING_GAP)
    assert issue.scope == HierarchyIssueScope.CATALOG_TITLE
    assert issue.catalog_title is title
    assert result.status == "review_required"


def test_two_primary_canonical_episodes_have_one_video_scoped_duplicate_issue():
    collection = _collection()
    title = _title(collection)
    first = _video(collection, title, 1, "Copy A.mkv", episode_number=5)
    second = _video(collection, title, 2, "Copy B.mkv", episode_number=5)

    result = evaluate_collection_hierarchy(collection, [first, second])

    issues = [item for item in result.issues if item.code == HierarchyIssueCode.CANONICAL_DUPLICATE]
    assert len(issues) == 1
    assert issues[0].scope == HierarchyIssueScope.VIDEO
    assert issues[0].videos == (first, second)


def test_fractional_video_has_stable_nonstandard_video_issue():
    collection = _collection()
    title = _title(collection)
    video = _video(collection, title, 1, "Episode 04.5.mkv")

    result = evaluate_collection_hierarchy(collection, [video])

    issue = next(item for item in result.issues if item.code == HierarchyIssueCode.NONSTANDARD_NUMBERING)
    assert issue.scope == HierarchyIssueScope.VIDEO
    assert issue.videos == (video,)
    assert result.status == "review_required"


def test_unassigned_video_has_stable_video_issue():
    collection = _collection()
    video = _video(collection, None, 1, "Episode 01.mkv", episode_number=1)

    result = evaluate_collection_hierarchy(collection, [video])

    issue = next(
        item for item in result.issues
        if item.code == HierarchyIssueCode.UNASSIGNED_VIDEO
    )
    assert issue.scope == HierarchyIssueScope.VIDEO
    assert issue.catalog_title is None
    assert issue.videos == (video,)
    assert result.status == "review_required"


def test_generic_type_and_missing_part_number_are_title_scoped():
    generic_collection = _collection()
    generic = _title(generic_collection, part_type="title", season_number=None)
    generic_result = evaluate_collection_hierarchy(generic_collection, [])
    generic_issue = next(
        item for item in generic_result.issues
        if item.code == HierarchyIssueCode.GENERIC_STRUCTURAL_TYPE
    )
    assert generic_issue.catalog_title is generic
    assert generic_issue.scope == HierarchyIssueScope.CATALOG_TITLE

    part_collection = _collection()
    part = _title(
        part_collection,
        part_type="part",
        season_number=1,
        part_number=None,
    )
    part_result = evaluate_collection_hierarchy(part_collection, [])
    part_issue = next(
        item for item in part_result.issues
        if item.code == HierarchyIssueCode.MISSING_PART_NUMBER
    )
    assert part_issue.catalog_title is part
    assert part_issue.scope == HierarchyIssueScope.CATALOG_TITLE


def test_incomplete_historical_part_is_not_verified_and_is_not_repaired():
    collection = _collection(status="verified")
    timestamp = utc_now()
    title = _title(
        collection,
        part_type="part",
        season_number=1,
        part_number=2,
        part_type_manual="part",
        season_number_manual=1,
        part_number_manual=None,
        hierarchy_manual_override=True,
        hierarchy_verified_at=timestamp,
    )

    result = evaluate_collection_hierarchy(collection, [])

    assert result.status == "review_required"
    assert HierarchyIssueCode.INCOMPLETE_MANUAL_SNAPSHOT in _codes(result)
    assert next(
        issue for issue in result.issues
        if issue.code == HierarchyIssueCode.INCOMPLETE_MANUAL_SNAPSHOT
    ).blocking is True
    assert title.part_number_manual is None
    assert title.hierarchy_verified_at == timestamp
    assert manual_hierarchy_snapshot_uses_legacy_projection(title) is True
    assert (
        title.effective_part_type,
        title.effective_season_number,
        title.effective_part_number,
    ) == ("part", 1, 2)


def test_complete_manual_season_without_problems_is_verified():
    collection = _collection()
    _title(
        collection,
        part_type_manual="season",
        season_number_manual=1,
        season_label_manual="S1",
        hierarchy_manual_override=True,
        hierarchy_verified_at=utc_now(),
    )

    result = evaluate_collection_hierarchy(collection, [])

    assert result.blocking_issues == ()
    assert result.status == "verified"


def test_inactive_manual_values_are_not_effective_authority():
    collection = _collection()
    title = _title(
        collection,
        part_type="season",
        season_number=2,
        part_type_manual="ova",
        season_number_manual=9,
        season_label_manual="stale",
        sort_order_manual=99,
        hierarchy_manual_override=False,
    )

    result = evaluate_collection_hierarchy(collection, [])

    assert manual_hierarchy_authority_state(title) == ManualHierarchyAuthorityState.NONE
    assert manual_hierarchy_snapshot_uses_legacy_projection(title) is False
    assert (
        title.effective_part_type,
        title.effective_season_number,
        title.effective_season_label,
        title.effective_sort_order,
    ) == ("season", 2, "S2", title.sort_order)
    assert result.status == "automatic"


def test_historical_verification_marker_is_preserved_without_legacy_projection():
    collection = _collection()
    title = _title(
        collection,
        part_type="season",
        season_number=2,
        part_type_manual="ova",
        season_number_manual=9,
        hierarchy_manual_override=False,
        hierarchy_verified_at=utc_now(),
    )

    result = evaluate_collection_hierarchy(collection, [])

    assert (
        manual_hierarchy_authority_state(title)
        == ManualHierarchyAuthorityState.INCOMPLETE
    )
    assert manual_hierarchy_snapshot_requires_preservation(title) is True
    assert manual_hierarchy_snapshot_uses_legacy_projection(title) is False
    assert (title.effective_part_type, title.effective_season_number) == (
        "season",
        2,
    )
    assert result.status == "review_required"


def test_confirmed_secondary_is_excluded_from_count_but_remains_review_issue():
    collection = _collection()
    title = _title(collection)
    primary = _video(collection, title, 1, "Primary.mkv", episode_number=1)
    secondary = _video(
        collection,
        title,
        2,
        "Secondary.mkv",
        episode_number=1,
        duplicate_of=primary,
        duplicate_of_video_id=primary.id,
    )

    summary = summarize_title_numbering([primary, secondary], title)
    result = evaluate_collection_hierarchy(collection, [primary, secondary])

    assert summary.standard_total == 1
    assert summary.numbered == 1
    assert HierarchyIssueCode.CANONICAL_DUPLICATE not in _codes(result)
    assert HierarchyIssueCode.CONFIRMED_DUPLICATE in _codes(result)
    assert result.status == "review_required"


def test_missing_duplicate_primary_has_stable_video_issue():
    collection = _collection()
    title = _title(collection)
    video = _video(
        collection,
        title,
        1,
        "Orphaned duplicate.mkv",
        episode_number=1,
        duplicate_primary_missing=True,
    )

    result = evaluate_collection_hierarchy(collection, [video])

    issue = next(
        item for item in result.issues
        if item.code == HierarchyIssueCode.DUPLICATE_PRIMARY_MISSING
    )
    assert issue.scope == HierarchyIssueScope.VIDEO
    assert issue.videos == (video,)


def test_stale_persisted_review_uses_explicit_legacy_fallback():
    collection = _collection(
        status="review_required",
        note="Historický důvod, který již nelze reprodukovat.",
    )
    _title(collection)

    result = evaluate_collection_hierarchy(collection, [])

    assert result.status == "review_required"
    assert len(result.blocking_issues) == 1
    issue = result.blocking_issues[0]
    assert issue.code == HierarchyIssueCode.LEGACY_UNLOCALIZED_REVIEW_STATE
    assert issue.scope == HierarchyIssueScope.COLLECTION
    assert issue.message == collection.hierarchy_note


def test_status_and_scope_do_not_depend_on_user_facing_message():
    collection = _collection()
    title = _title(collection, part_type="title", season_number=None)
    result = evaluate_collection_hierarchy(collection, [])
    original = result.blocking_issues[0]
    translated = replace(original, message="Zcela jiný uživatelský text.")

    assert derive_hierarchy_status(collection, (original,)) == "review_required"
    assert derive_hierarchy_status(collection, (translated,)) == "review_required"
    assert translated.code == original.code
    assert translated.scope == original.scope
    assert translated.catalog_title is title


def test_diagnostics_are_a_presentation_projection_of_evaluation():
    collection = _collection()
    title = _title(collection)
    video = _video(collection, title, 1, "Episode 04.5.mkv")
    evaluation = evaluate_collection_hierarchy(collection, [video])

    diagnostics = hierarchy_review_diagnostics(collection, [video], evaluation)

    assert diagnostics.evaluation is evaluation
    assert tuple(item.issue for item in diagnostics.issues) == evaluation.issues
    assert diagnostics.blocking_count == len(evaluation.blocking_issues)
    assert diagnostics.for_video(video)[0].issue is evaluation.blocking_issues[0]
