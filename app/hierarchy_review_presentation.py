from __future__ import annotations

from dataclasses import dataclass

from .models import CatalogCollection, CatalogTitle
from .numbering import TitleNumberingSummary, summarize_title_numbering
from .structural_inference import automatic_flat_sequence_notice
from .supplementary import SupplementaryReviewIssue, supplementary_review_issues


@dataclass(frozen=True)
class SupplementaryReviewPresentation:
    title: CatalogTitle
    issues: tuple[SupplementaryReviewIssue, ...]


@dataclass(frozen=True)
class HierarchyReviewBadge:
    key: str
    label: str
    severity: str


@dataclass(frozen=True)
class HierarchyReviewCollectionPresentation:
    badge: HierarchyReviewBadge
    numbering_unknown: int
    supplementary_reviews: tuple[SupplementaryReviewPresentation, ...]
    requires_review: bool


REVIEW_REQUIRED_BADGE = HierarchyReviewBadge(
    key="review_required",
    label="Vyžaduje kontrolu",
    severity="warning",
)
LONG_EPISODE_SET_BADGE = HierarchyReviewBadge(
    key="long_episode_set",
    label="Zvláštně dlouhá sada epizod",
    severity="warning",
)
VERIFIED_BADGE = HierarchyReviewBadge(
    key="verified",
    label="Ověřeno",
    severity="success",
)
AUTOMATIC_OK_BADGE = HierarchyReviewBadge(
    key="automatic_ok",
    label="Automaticky OK",
    severity="success",
)


def build_hierarchy_review_collection_presentation(
    collection: CatalogCollection,
) -> HierarchyReviewCollectionPresentation:
    """Resolve the overview queue and navigation badge from loaded ORM state.

    The queue predicate intentionally remains identical to the existing
    Hierarchy Review overview.  Supplementary ordinal issues are derived and
    therefore override even a stored verified/automatic collection status.
    The long-set badge reuses the existing non-blocking structural notice; a
    blocking long sequence is already represented by review_required.
    """
    summaries: list[TitleNumberingSummary] = []
    supplementary_reviews = []
    has_long_episode_set = False
    for title in collection.titles:
        title_videos = list(title.videos)
        summaries.append(summarize_title_numbering(title_videos, title))
        issues = supplementary_review_issues(title_videos, title)
        if issues:
            supplementary_reviews.append(SupplementaryReviewPresentation(
                title=title,
                issues=issues,
            ))
        if automatic_flat_sequence_notice(title, title_videos) is not None:
            has_long_episode_set = True

    requires_review = bool(
        collection.hierarchy_status in {"review_required", "conflict"}
        or any(summary.requires_review for summary in summaries)
        or supplementary_reviews
    )
    if requires_review:
        badge = REVIEW_REQUIRED_BADGE
    elif has_long_episode_set:
        badge = LONG_EPISODE_SET_BADGE
    elif collection.hierarchy_status == "verified":
        badge = VERIFIED_BADGE
    else:
        badge = AUTOMATIC_OK_BADGE

    return HierarchyReviewCollectionPresentation(
        badge=badge,
        numbering_unknown=sum(summary.unknown for summary in summaries),
        supplementary_reviews=tuple(supplementary_reviews),
        requires_review=requires_review,
    )
