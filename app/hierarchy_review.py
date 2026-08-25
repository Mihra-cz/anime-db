from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
from pathlib import PurePosixPath
import posixpath
import re

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, selectinload

from .catalog import GENERIC_ROOTS, detect_episode_number, normalize_title
from .hierarchy import parse_explicit_part
from .hierarchy_authority import (
    activate_manual_hierarchy_snapshot,
    clear_manual_hierarchy_snapshot,
    structural_hierarchy_issue,
)
from .hierarchy_evaluation import (
    CONFIRMED_DUPLICATES_REVIEW_REASON,
    FILENAME_SEASON_CONFLICT_REVIEW_REASON,
    MISSING_DUPLICATE_PRIMARY_REVIEW_REASON,
    MISSING_PART_NUMBER_REVIEW_REASON,
    NONSTANDARD_NUMBERING_REVIEW_REASON,
    NUMBERING_REVIEW_SUMMARY,
    UNNUMBERED_SUPPLEMENTARY_REVIEW_REASON,
    HierarchyEvaluationResult,
    HierarchyIssue,
    HierarchyIssueScope,
    catalog_title_hierarchy_is_verified,
    evaluate_collection_hierarchy,
    finalize_hierarchy_write,
    hierarchy_primary_note,
    manual_hierarchy_snapshot_issue,
)
from .hierarchy_types import PART_TYPE_LABELS, PART_TYPES, VIDEO_CONTENT_TYPES
from .hierarchy_provenance import (
    RELATED_NAMED_CHILD_REVIEW_REASON as PROBABLE_GROUPING_REVIEW_REASON,
    SUPPLEMENTARY_NAMED_CHILD_REVIEW_REASON as SUPPLEMENTARY_CONTEXT_REVIEW_REASON,
)
from .manual_split import (
    AssignmentPreview,
    ManualTitleDefinition,
    apply_manual_split_decisions,
    compile_manual_split_pattern,
    definition_from_title,
    evaluate_manual_split_assignment,
    has_persisted_manual_split_selector,
    replace_explicit_video_selector_authority,
    synchronize_manual_split_authority,
)
from .models import (
    CatalogCollection, CatalogTitle, CollectionGroupingDecision,
    ManualSplitRuleVideo, Video, utc_now,
)
from .numbering import (
    clear_duplicate_group, effective_video_numbering,
    set_duplicate_group_primary,
    supplementary_context_map, video_numbering_identity,
)
from .structural_inference import (
    GENERIC_TITLE_REVIEW_REASON,
    LONG_FLAT_SEQUENCE_REVIEW_REASON,
    direct_root_episode_profile,
)


PERIOD_HINT = re.compile(
    r"(?:\(\s*|\s)([A-Z]\d{2}(?:-[A-Z]\d{2})?)(?:\s*\)|\s*$)",
    re.IGNORECASE,
)
# Zachováno pouze pro rozpoznání dříve persistované poznámky v administračním
# formuláři. collection_requires_review už legacy časový hint jako důvod nevrací.
PERIOD_HINT_REVIEW_REASON = (
    "Interní časový rozsah neurčuje bezpečně hranice sezón nebo částí."
)
SUPPLEMENTAL_PART_TYPES = {"film", "ova", "special", "preview", "recap", "bonus", "other"}
MANUAL_DUPLICATE_STATUSES = {"suspected"}
ALLOWED_NUMBERING_MODES = {"unknown", "season_local", "absolute", "mixed"}
SIMPLE_DEFINITION_FIELDS = (
    "title_id", "local_title", "manual_display_title", "season_number_manual",
    "season_label_manual", "part_number_manual", "part_type_manual", "episode_start",
    "episode_end", "episode_start_offset", "numbering_mode", "sort_order",
    "filename_pattern", "video_ids",
)


@dataclass(frozen=True)
class HierarchyReviewIssue:
    """Presentation metadata for one authoritative structured issue."""

    issue: HierarchyIssue
    anchor_id: str = "hierarchy-collection-issues"

    @property
    def code(self) -> str:
        return self.issue.code.value

    @property
    def message(self) -> str:
        return self.issue.message

    @property
    def scope(self) -> str:
        return self.issue.scope.value

    @property
    def blocking(self) -> bool:
        return self.issue.blocking

    @property
    def catalog_title(self) -> CatalogTitle | None:
        return self.issue.catalog_title

    @property
    def videos(self) -> tuple[Video, ...]:
        return self.issue.videos

    @property
    def related_catalog_titles(self) -> tuple[CatalogTitle, ...]:
        return self.issue.related_catalog_titles

    @property
    def title_id(self) -> int | None:
        return self.catalog_title.id if self.catalog_title is not None else None

    @property
    def video_ids(self) -> tuple[int, ...]:
        return tuple(video.id for video in self.videos if video.id is not None)


@dataclass(frozen=True)
class HierarchyReviewDiagnostics:
    """Read model shared by Hierarchy Review summaries and object cards."""

    evaluation: HierarchyEvaluationResult
    issues: tuple[HierarchyReviewIssue, ...]
    collection_issues: tuple[HierarchyReviewIssue, ...]
    title_issues: Mapping[int, tuple[HierarchyReviewIssue, ...]]
    video_issues: Mapping[int, tuple[HierarchyReviewIssue, ...]]

    @property
    def blocking_issues(self) -> tuple[HierarchyReviewIssue, ...]:
        return tuple(issue for issue in self.issues if issue.blocking)

    @property
    def blocking_count(self) -> int:
        return len(self.blocking_issues)

    @property
    def affected_title_ids(self) -> tuple[int, ...]:
        ids = {
            title_id
            for issue in self.blocking_issues
            for title_id in (
                issue.title_id,
                *(title.id for title in issue.related_catalog_titles),
                *(
                    video.catalog_title_id
                    for video in issue.videos
                    if video.catalog_title_id is not None
                ),
            )
            if title_id is not None
        }
        return tuple(sorted(ids))

    @property
    def affected_title_count(self) -> int:
        return len(self.affected_title_ids)

    @property
    def affected_video_ids(self) -> tuple[int, ...]:
        return tuple(sorted({
            video_id
            for issue in self.blocking_issues
            for video_id in issue.video_ids
        }))

    @property
    def affected_video_count(self) -> int:
        return len(self.affected_video_ids)

    def for_title(
        self, title: CatalogTitle | int,
    ) -> tuple[HierarchyReviewIssue, ...]:
        title_id = title if isinstance(title, int) else title.id
        if title_id is not None:
            return self.title_issues.get(title_id, ())
        return tuple(
            issue for issue in self.issues
            if issue.catalog_title is title
        )

    def for_video(self, video: Video | int) -> tuple[HierarchyReviewIssue, ...]:
        video_id = video if isinstance(video, int) else video.id
        if video_id is not None:
            return self.video_issues.get(video_id, ())
        return tuple(
            issue for issue in self.issues
            if not isinstance(video, int) and video in issue.videos
        )

    def for_title_card(
        self, title: CatalogTitle | int,
    ) -> tuple[HierarchyReviewIssue, ...]:
        title_id = title if isinstance(title, int) else title.id
        direct = list(self.for_title(title))
        seen = {id(issue) for issue in direct}
        for issue in self.issues:
            if id(issue) in seen:
                continue
            belongs_to_title = any(
                video.catalog_title_id == title_id
                if title_id is not None
                else (
                    not isinstance(title, int)
                    and video.catalog_title is title
                )
                for video in issue.videos
            )
            if belongs_to_title:
                direct.append(issue)
                seen.add(id(issue))
        return tuple(direct)

    def title_has_blocking_issue(self, title: CatalogTitle | int) -> bool:
        return any(issue.blocking for issue in self.for_title_card(title))

    def video_has_blocking_issue(self, video: Video | int) -> bool:
        return any(issue.blocking for issue in self.for_video(video))


@dataclass(frozen=True)
class SingleTitleConfirmationSuggestion:
    title: CatalogTitle
    metadata_supports_tv: bool
    proposed_part_type: str
    proposed_season_number: int | None
    proposed_season_label: str | None

    @property
    def display_label(self) -> str:
        label = PART_TYPE_LABELS.get(
            self.proposed_part_type, self.proposed_part_type.replace("_", " ").title()
        )
        if self.proposed_part_type != "season":
            return label
        # The recommendation has historically used this concise English label.
        label = "Season"
        season_label = self.proposed_season_label or (
            f"S{self.proposed_season_number}"
            if self.proposed_season_number is not None else None
        )
        if self.proposed_season_number is not None:
            label = f"{label} {self.proposed_season_number}"
        return f"{label} ({season_label})" if season_label else label


@dataclass(frozen=True)
class CollectionGroupingSuggestion:
    key: str
    state_fingerprint: str
    proposed_name: str
    collections: tuple[CatalogCollection, ...]
    target_collection_id: int | None
    reasons: tuple[str, ...]

    @property
    def title_ids(self) -> tuple[int, ...]:
        return tuple(
            title.id for collection in self.collections for title in collection.titles
        )


@dataclass
class CollectionGroupingMetrics:
    ancestor_lookups: int = 0
    sibling_name_comparisons: int = 0

    @property
    def candidate_comparisons(self) -> int:
        return self.ancestor_lookups + self.sibling_name_comparisons


@dataclass(frozen=True)
class EmptyCollectionDeleteResult:
    deleted: tuple[tuple[int, str], ...]
    skipped: tuple[tuple[int, str], ...]


@dataclass(frozen=True)
class SupplementaryVideoSuggestion:
    video: Video
    supplementary_type: str
    supplementary_number: int
    display_label: str
    context_label: str | None
    context_season_number: int | None
    proposed_part_type: str
    proposed_title: str


@dataclass(frozen=True)
class SupplementaryAssignmentItem:
    video: Video
    filename_episode_hint: int | None
    title_candidate: str | None
    supplementary_number: int | None
    season_hint: int | None

    @property
    def filename_hint_label(self) -> str | None:
        if self.season_hint is not None and self.filename_episode_hint is not None:
            return f"S{self.season_hint:02d}E{self.filename_episode_hint:02d}"
        return None


@dataclass(frozen=True)
class SupplementaryAssignmentRecommendation:
    items: tuple[SupplementaryAssignmentItem, ...]
    supplementary_type: str
    proposed_part_type: str
    type_label: str
    proposed_title: str
    season_number: int | None

    @property
    def video_ids(self) -> tuple[int, ...]:
        return tuple(item.video.id for item in self.items)

    @property
    def season_display(self) -> str | None:
        return f"S{self.season_number:02d}" if self.season_number is not None else None

    @property
    def season_label(self) -> str | None:
        return f"S{self.season_number}" if self.season_number is not None else None

    @property
    def canonical_numbering_known(self) -> bool:
        return all(item.supplementary_number is not None for item in self.items)

    @property
    def anchor_id(self) -> str:
        return f"assignment-recommendation-{self.items[0].video.id}"


SUPPLEMENTARY_ASSIGNMENT_LABELS = {
    "special": ("special", "Special", "Specials"),
    "ova": ("ova", "OVA", "OVA"),
    "preview": ("preview", "Preview", "Preview"),
    "recap": ("recap", "Recap", "Recap"),
    "ncop": ("bonus", "NCOP", "NCOP"),
    "nced": ("bonus", "NCED", "NCED"),
    "op": ("bonus", "OP", "OP"),
    "ed": ("bonus", "ED", "ED"),
    "bonus": ("bonus", "Bonus", "Bonus"),
    "cm": ("bonus", "CM", "CM"),
    "menu": ("bonus", "Menu", "Menu"),
}


def supplementary_assignment_recommendations(
    videos: list[Video],
) -> tuple[SupplementaryAssignmentRecommendation, ...]:
    """Navrhne pouze předvyplnění univerzální správy zařazení; nic nemění."""
    grouped: dict[
        tuple[int, str, int | None, str], list[SupplementaryAssignmentItem]
    ] = {}
    labels: dict[tuple[int, str, int | None, str], tuple[str, str, str]] = {}
    for video in videos:
        current = video.catalog_title
        if (
            video.content_type_manual
            or current is None
            or current.effective_part_type != "season"
        ):
            continue
        detection = detect_episode_number(video.filename)
        if not detection.is_supplementary or not detection.supplementary_type:
            continue
        part_type, type_label, proposed_title = SUPPLEMENTARY_ASSIGNMENT_LABELS.get(
            detection.supplementary_type,
            ("bonus", "Doplňkový obsah", "Bonus"),
        )
        key = (
            current.id, detection.supplementary_type,
            detection.season_hint, proposed_title,
        )
        grouped.setdefault(key, []).append(SupplementaryAssignmentItem(
            video=video,
            filename_episode_hint=detection.filename_episode_hint,
            title_candidate=detection.title_candidate,
            supplementary_number=detection.supplementary_number,
            season_hint=detection.season_hint,
        ))
        labels[key] = (part_type, type_label, proposed_title)
    return tuple(
        SupplementaryAssignmentRecommendation(
            items=tuple(sorted(items, key=lambda item: item.video.relative_path)),
            supplementary_type=key[1], proposed_part_type=labels[key][0],
            type_label=labels[key][1], proposed_title=labels[key][2],
            season_number=key[2],
        )
        for key, items in sorted(
            grouped.items(),
            key=lambda item: (
                item[0][2] is None, item[0][2] or 0, item[0][1],
                item[1][0].video.relative_path,
            ),
        )
    )


def set_manual_duplicate_status(video: Video, status: str | None) -> None:
    """Uloží pouze ruční podezření; potvrzený duplicate vztah zůstává oddělený."""
    normalized = status.strip().casefold() if status else None
    if normalized not in MANUAL_DUPLICATE_STATUSES | {None}:
        raise ValueError("Neplatný stav ručního podezření na duplicitu.")
    video.duplicate_status_manual = normalized


def supplementary_video_suggestion(
    video: Video, *, title_names: dict[str, list[CatalogTitle]] | None = None,
    include_already_supplementary: bool = False,
) -> SupplementaryVideoSuggestion | None:
    if video.content_type_manual:
        return None
    detection = detect_episode_number(video.filename)
    if not detection.is_supplementary or detection.supplementary_number is None:
        return None
    current = video.catalog_title
    identity = video_numbering_identity(video, title_names=title_names)
    if identity is None:
        return None
    context_label = identity.context_label
    current_is_supplementary = bool(
        current and current.effective_part_type in SUPPLEMENTAL_PART_TYPES
    )
    context_already_present = bool(
        current and context_label
        and normalize_title(context_label) in normalize_title(current.local_title)
    )
    if not include_already_supplementary and current_is_supplementary and (
        not context_label or context_already_present
    ):
        return None
    part_type = {
        "ova": "ova", "special": "special", "preview": "preview", "recap": "recap",
    }.get(identity.supplementary_type or "", "bonus")
    type_label = {
        "ova": "OVA", "special": "Specials", "ncop": "NCOP", "nced": "NCED",
        "op": "OP", "ed": "ED", "preview": "Preview", "recap": "Recap",
        "bonus": "Bonus", "cm": "CM", "menu": "Menu",
    }.get(identity.supplementary_type or "", "Doplňkový obsah")
    proposed_title = f"{type_label} – {context_label}" if context_label else type_label
    return SupplementaryVideoSuggestion(
        video, identity.supplementary_type or "bonus", identity.number,
        detection.display_value or video.filename, context_label,
        identity.context_season_number, part_type, proposed_title,
    )


def supplementary_video_suggestions(
    videos: list[Video], *, include_video_ids: set[int] | None = None,
) -> tuple[SupplementaryVideoSuggestion, ...]:
    title_names = supplementary_context_map(videos)
    included = include_video_ids or set()
    return tuple(
        suggestion for video in videos
        if (
            suggestion := supplementary_video_suggestion(
                video, title_names=title_names,
                include_already_supplementary=video.id in included,
            )
        ) is not None
    )


def _digest(values: list[str]) -> str:
    return hashlib.sha256("\n".join(values).encode("utf-8")).hexdigest()


def _grouping_name_base(value: str) -> str:
    text = re.sub(r"\s*\([^)]*\)\s*$", "", value).strip()
    text = re.sub(
        r"\s+(?:[IVXLCDM]+|season\s*\d+|s\s*\d+|part\s*\d+)$",
        "", text, flags=re.IGNORECASE,
    ).strip()
    return normalize_title(text)


def _related_name_bases(left: str, right: str) -> bool:
    if not left or not right or min(len(left), len(right)) < 5:
        return False
    return left == right or left.startswith(f"{right} ") or right.startswith(f"{left} ")


def _is_structural_collection(collection: CatalogCollection) -> bool:
    return parse_explicit_part(collection.local_title) is not None


def _suggestion_state(collections: list[CatalogCollection]) -> str:
    values = []
    for collection in sorted(collections, key=lambda item: item.relative_root_path):
        values.append(f"collection:{collection.id}:{collection.relative_root_path}")
        for title in sorted(collection.titles, key=lambda item: item.id):
            values.append(
                f"title:{title.id}:{title.catalog_collection_id}:{title.relative_root_path}"
            )
            for video in sorted(title.videos, key=lambda item: item.id):
                values.append(
                    f"video:{video.id}:{video.relative_path}:{video.duplicate_of_video_id}:"
                    f"{int(video.duplicate_primary_missing)}"
                )
    return _digest(values)


def collection_grouping_suggestions(
    session: Session, *, collections: list[CatalogCollection] | None = None,
    metrics: CollectionGroupingMetrics | None = None,
) -> list[CollectionGroupingSuggestion]:
    """Vrátí konzervativní návrhy; samotná podobnost bez ancestry nestačí."""
    metrics = metrics or CollectionGroupingMetrics()
    if collections is None:
        collections = list(session.scalars(select(CatalogCollection).options(
            selectinload(CatalogCollection.titles).selectinload(CatalogTitle.videos),
        )).all())
    active = [collection for collection in collections if collection.titles]
    by_path = {collection.relative_root_path: collection for collection in active}
    adjacency: dict[int, set[int]] = {collection.id: set() for collection in active}
    reasons_by_node: dict[int, set[str]] = {collection.id: set() for collection in active}
    paths = {
        collection.id: PurePosixPath(collection.relative_root_path)
        for collection in active
    }
    name_bases = {
        collection.id: _grouping_name_base(collection.local_title)
        for collection in active
    }
    structural = {
        collection.id: _is_structural_collection(collection)
        for collection in active
    }

    def connect(first: CatalogCollection, second: CatalogCollection, reason: str) -> None:
        adjacency[first.id].add(second.id)
        adjacency[second.id].add(first.id)
        reasons_by_node[first.id].add(reason)
        reasons_by_node[second.id].add(reason)

    by_parent: dict[str, list[CatalogCollection]] = {}
    for collection in active:
        path = paths[collection.id]
        parent = path.parent.as_posix()
        by_parent.setdefault(parent, []).append(collection)
        ancestor_path = path.parent
        while ancestor_path.as_posix() not in {".", "/"}:
            metrics.ancestor_lookups += 1
            ancestor = by_path.get(ancestor_path.as_posix())
            if ancestor is not None and (
                structural[collection.id]
                or _related_name_bases(name_bases[ancestor.id], name_bases[collection.id])
            ):
                connect(
                    ancestor, collection,
                    "collection leží pod fyzickým rootem druhé collection",
                )
            ancestor_path = ancestor_path.parent

    for parent, siblings in by_parent.items():
        parent_name = PurePosixPath(parent).name.casefold()
        if parent in {".", "/"} or parent_name in GENERIC_ROOTS:
            continue
        related_nodes: set[int] = set()
        by_first_token: dict[str, list[CatalogCollection]] = {}
        for collection in siblings:
            base = name_bases[collection.id]
            first_token = base.split(maxsplit=1)[0] if base else ""
            if first_token:
                by_first_token.setdefault(first_token, []).append(collection)
        for bucket in by_first_token.values():
            for index, first in enumerate(bucket):
                for second in bucket[index + 1:]:
                    metrics.sibling_name_comparisons += 1
                    if not _related_name_bases(name_bases[first.id], name_bases[second.id]):
                        continue
                    connect(first, second, "společný fyzický parent a příbuzný základ názvu")
                    related_nodes.update((first.id, second.id))
        structural_siblings = [item for item in siblings if structural[item.id]]
        anchors = [item for item in siblings if item.id in related_nodes]
        if structural_siblings:
            anchors = anchors or structural_siblings
            for item in structural_siblings:
                for anchor in anchors:
                    if item is not anchor:
                        connect(
                            item, anchor,
                            "supplementary/season-like složka má stejný anime parent",
                        )

    decisions = {
        decision.suggestion_key: decision
        for decision in session.scalars(select(CollectionGroupingDecision)).all()
    }
    by_id = {collection.id: collection for collection in active}
    suggestions = []
    visited: set[int] = set()
    for collection in active:
        if collection.id in visited or not adjacency[collection.id]:
            continue
        stack, component_ids = [collection.id], set()
        while stack:
            current = stack.pop()
            if current in component_ids:
                continue
            component_ids.add(current)
            stack.extend(adjacency[current] - component_ids)
        visited.update(component_ids)
        component = sorted(
            (by_id[item_id] for item_id in component_ids),
            key=lambda item: item.relative_root_path,
        )
        component_paths = [item.relative_root_path for item in component]
        key = _digest(component_paths)
        fingerprint = _suggestion_state(component)
        prior = decisions.get(key)
        if prior is not None and prior.state_fingerprint == fingerprint:
            continue
        common_parent = PurePosixPath(posixpath.commonpath(component_paths) or ".")
        direct_target = next((
            item for item in component
            if all(
                other is item
                or other.relative_root_path.startswith(f"{item.relative_root_path}/")
                for other in component
            )
        ), None)
        proposed_name = (
            direct_target.local_title if direct_target is not None
            else common_parent.name if common_parent.name.casefold() not in GENERIC_ROOTS | {"."}
            else min(component, key=lambda item: len(_grouping_name_base(item.local_title))).local_title
        )
        component_reasons = sorted({
            reason for item_id in component_ids for reason in reasons_by_node[item_id]
        })
        suggestions.append(CollectionGroupingSuggestion(
            key=key, state_fingerprint=fingerprint, proposed_name=proposed_name,
            collections=tuple(component),
            target_collection_id=direct_target.id if direct_target else None,
            reasons=tuple(component_reasons),
        ))
    return sorted(suggestions, key=lambda item: item.proposed_name.casefold())


def record_grouping_decision(
    session: Session, suggestion: CollectionGroupingSuggestion, decision: str,
) -> None:
    if decision not in {"separate", "merged"}:
        raise ValueError("Neplatné rozhodnutí o seskupení.")
    stored = session.scalar(select(CollectionGroupingDecision).where(
        CollectionGroupingDecision.suggestion_key == suggestion.key
    ))
    if stored is None:
        stored = CollectionGroupingDecision(suggestion_key=suggestion.key)
        session.add(stored)
    stored.state_fingerprint = suggestion.state_fingerprint
    stored.decision = decision


def _unique_manual_collection_path(session: Session, name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", normalize_title(name)).strip("-") or "collection"
    base = f"@manual/{slug}"
    path, suffix = base, 1
    while session.scalar(select(CatalogCollection.id).where(
        CatalogCollection.relative_root_path == path
    )) is not None:
        suffix += 1
        path = f"{base}-{suffix}"
    return path


def create_main_collection(
    session: Session, local_title: str, title_ids: list[int],
) -> CatalogCollection:
    name = local_title.strip()[:200]
    if not name:
        raise ValueError("Hlavní collection musí mít lokální název.")
    collection = CatalogCollection(
        local_title=name, normalized_local_title=normalize_title(name),
        relative_root_path=_unique_manual_collection_path(session, name),
        manual_display_title=name,
    )
    session.add(collection)
    session.flush()
    move_titles_to_collection(session, collection.id, title_ids)
    return collection


def move_titles_to_collection(
    session: Session, target_collection_id: int, title_ids: list[int],
) -> CatalogCollection:
    selected_ids = {int(value) for value in title_ids}
    if not selected_ids:
        raise ValueError("Vyberte alespoň jednu část.")
    target = session.scalar(select(CatalogCollection).options(
        selectinload(CatalogCollection.titles).selectinload(CatalogTitle.videos),
        selectinload(CatalogCollection.videos),
    ).where(CatalogCollection.id == target_collection_id))
    if target is None:
        raise ValueError("Cílová collection nebyla nalezena.")
    titles = list(session.scalars(select(CatalogTitle).options(
        selectinload(CatalogTitle.videos), selectinload(CatalogTitle.collection),
    ).where(CatalogTitle.id.in_(selected_ids))).all())
    if len(titles) != len(selected_ids):
        raise ValueError("Výběr obsahuje cizí nebo neexistující část.")
    sources = {title.collection for title in titles if title.collection is not None}
    for title in titles:
        for link in title.manual_split_rule_videos:
            resulting_collections = {
                target
                if other.catalog_title_id in selected_ids
                else other.catalog_title.collection
                for other in link.video.manual_split_rule_videos
                if other.catalog_title is not None
            }
            if len(resulting_collections) > 1 or None in resulting_collections:
                raise ValueError(
                    "Část nelze přesunout bez dalších částí, se kterými sdílí "
                    "explicitní selector authority."
                )
    for title in titles:
        title.collection = target
        for video in title.videos:
            video.catalog_collection = target
        for link in title.manual_split_rule_videos:
            link.video.catalog_collection = target
    session.flush()
    affected = sources | {target}
    for collection in affected:
        session.expire(collection, ["titles", "videos"])
    finalize_hierarchy_write(list(affected))
    session.flush()
    return target


def delete_empty_collection(session: Session, collection_id: int) -> None:
    collection = session.get(CatalogCollection, collection_id)
    if collection is None:
        raise ValueError("Collection nebyla nalezena.")
    title_count = session.scalar(select(func.count(CatalogTitle.id)).where(
        CatalogTitle.catalog_collection_id == collection_id
    )) or 0
    video_count = session.scalar(select(func.count(Video.id)).where(
        Video.catalog_collection_id == collection_id
    )) or 0
    if title_count or video_count:
        raise ValueError("Odstranit lze pouze collection bez částí a videí.")
    deleted = session.execute(delete(CatalogCollection).where(
        CatalogCollection.id == collection_id,
        ~select(CatalogTitle.id).where(
            CatalogTitle.catalog_collection_id == CatalogCollection.id
        ).exists(),
        ~select(Video.id).where(
            Video.catalog_collection_id == CatalogCollection.id
        ).exists(),
    ).execution_options(synchronize_session=False))
    if deleted.rowcount != 1:
        raise ValueError(
            "Collection už není prázdná; byla změněna a nebyla odstraněna."
        )


def delete_empty_collections(
    session: Session, collection_ids: list[int],
) -> EmptyCollectionDeleteResult:
    selected_ids = tuple(dict.fromkeys(int(value) for value in collection_ids))
    if not selected_ids:
        raise ValueError("Vyberte alespoň jednu prázdnou collection.")
    collections = {
        collection.id: collection
        for collection in session.scalars(select(CatalogCollection).where(
            CatalogCollection.id.in_(selected_ids)
        )).all()
    }
    title_counts = dict(session.execute(
        select(CatalogTitle.catalog_collection_id, func.count(CatalogTitle.id))
        .where(CatalogTitle.catalog_collection_id.in_(selected_ids))
        .group_by(CatalogTitle.catalog_collection_id)
    ).all())
    video_counts = dict(session.execute(
        select(Video.catalog_collection_id, func.count(Video.id))
        .where(Video.catalog_collection_id.in_(selected_ids))
        .group_by(Video.catalog_collection_id)
    ).all())
    deleted, skipped = [], []
    for collection_id in selected_ids:
        collection = collections.get(collection_id)
        if collection is None:
            skipped.append((collection_id, "collection už neexistuje"))
        elif title_counts.get(collection_id, 0) or video_counts.get(collection_id, 0):
            skipped.append((collection_id, collection.local_title))
        else:
            deleted.append((collection_id, collection.local_title))
    if deleted:
        deletable_ids = [collection_id for collection_id, _ in deleted]
        session.execute(delete(CatalogCollection).where(
            CatalogCollection.id.in_(deletable_ids),
            ~select(CatalogTitle.id).where(
                CatalogTitle.catalog_collection_id == CatalogCollection.id
            ).exists(),
            ~select(Video.id).where(
                Video.catalog_collection_id == CatalogCollection.id
            ).exists(),
        ))
        session.flush()
        remaining_ids = set(session.scalars(select(CatalogCollection.id).where(
            CatalogCollection.id.in_(deletable_ids)
        )).all())
        if remaining_ids:
            skipped.extend(
                (collection_id, name) for collection_id, name in deleted
                if collection_id in remaining_ids
            )
            deleted = [
                item for item in deleted if item[0] not in remaining_ids
            ]
    return EmptyCollectionDeleteResult(tuple(deleted), tuple(skipped))


def extract_local_period_hint(local_title: str) -> str | None:
    matches = PERIOD_HINT.findall(local_title or "")
    return matches[-1].upper() if matches else None


def confirm_effective_collection_hierarchy(collection: CatalogCollection) -> None:
    """Persist the current concrete effective hierarchy as manual authority."""
    if not collection.titles:
        raise ValueError("Prázdnou collection nelze potvrdit jako hierarchii.")
    if any(title.effective_part_type == "title" for title in collection.titles):
        raise ValueError(
            "Hierarchii nelze potvrdit, dokud všechny části nemají konkrétní typ."
        )
    snapshots = []
    for title in collection.titles:
        _validate_structural_numbers(
            title.effective_part_type,
            title.effective_season_number,
            title.effective_part_number,
        )
        snapshots.append((
            title,
            title.effective_part_type,
            title.effective_season_number,
            title.effective_part_number,
            title.effective_season_label,
            title.effective_sort_order,
        ))
    now = utc_now()
    for title, part_type, season_number, part_number, label, order in snapshots:
        activate_manual_hierarchy_snapshot(
            title,
            part_type=part_type,
            season_number=season_number,
            part_number=part_number,
            season_label=label,
            sort_order=order,
            verified_at=now,
        )


def resolve_collection_hierarchy_status(
    collection: CatalogCollection, reason: str | None,
) -> None:
    """Compatibility entry point; status and note remain strictly derived."""
    finalize_hierarchy_write([collection])


def collection_requires_review(collection: CatalogCollection, videos: list[Video]) -> str | None:
    """Compatibility adapter for scanner/startup until lifecycle Commit 2.

    These callers intentionally keep their current pre-numbering order. They use
    the shared issue identities, but omit final-numbering rules that their current
    lifecycle cannot evaluate authoritatively yet.
    """
    result = evaluate_collection_hierarchy(
        collection,
        videos,
        include_legacy_fallback=False,
        include_unassigned=False,
        include_complete_numbering=False,
    )
    return hierarchy_primary_note(result.issues, summarize_numbering=False)


def _hierarchy_issue_anchor(
    code: str, *, title: CatalogTitle | None = None,
    videos: tuple[Video, ...] = (),
) -> str:
    video_ids = sorted(video.id for video in videos if video.id is not None)
    if video_ids:
        return f"video-issue-{video_ids[0]}-{code}"
    if title is not None and title.id is not None:
        return f"title-issue-{title.id}-{code}"
    return "hierarchy-collection-issues"


def hierarchy_review_diagnostics(
    collection: CatalogCollection,
    videos: list[Video],
    evaluation: HierarchyEvaluationResult | None = None,
) -> HierarchyReviewDiagnostics:
    """Group authoritative structured issues for templates and anchors only."""
    evaluation = evaluation or evaluate_collection_hierarchy(collection, videos)
    issues = [
        HierarchyReviewIssue(
            issue=issue,
            anchor_id=_hierarchy_issue_anchor(
                issue.code.value,
                title=issue.catalog_title,
                videos=issue.videos,
            ),
        )
        for issue in evaluation.issues
    ]
    collection_issues: list[HierarchyReviewIssue] = []
    title_issues: dict[int, list[HierarchyReviewIssue]] = {}
    video_issues: dict[int, list[HierarchyReviewIssue]] = {}
    for issue in issues:
        if issue.scope == HierarchyIssueScope.COLLECTION.value:
            collection_issues.append(issue)
        elif (
            issue.scope == HierarchyIssueScope.CATALOG_TITLE.value
            and issue.title_id is not None
        ):
            title_issues.setdefault(issue.title_id, []).append(issue)
        elif issue.scope == HierarchyIssueScope.VIDEO.value:
            for video_id in issue.video_ids:
                video_issues.setdefault(video_id, []).append(issue)

    return HierarchyReviewDiagnostics(
        evaluation=evaluation,
        issues=tuple(issues),
        collection_issues=tuple(collection_issues),
        title_issues={
            title_id: tuple(items) for title_id, items in title_issues.items()
        },
        video_issues={
            video_id: tuple(items) for video_id, items in video_issues.items()
        },
    )


def single_title_confirmation_suggestion(
    collection: CatalogCollection,
) -> SingleTitleConfirmationSuggestion | None:
    if (
        len(collection.titles) != 1
        or collection.hierarchy_status in {"verified", "conflict", "not_applicable"}
        or collection.hierarchy_verified_at is not None
    ):
        return None
    title = collection.titles[0]
    if (
        not title.videos
        or (title.part_type or "title") not in {"title", "season"}
        or title.part_type_manual is not None
        or title.season_number_manual is not None
        or title.season_label_manual is not None
        or title.sort_order_manual is not None
        or title.hierarchy_manual_override
        or title.hierarchy_verified_at is not None
    ):
        return None
    metadata_format = (
        title.metadata_record.format.strip().upper()
        if title.metadata_record and title.metadata_record.format else ""
    )
    profile = direct_root_episode_profile(list(title.videos))
    if (
        title.part_type != "season"
        and metadata_format not in {"TV", "TV_SHORT"}
        and not (profile.episode_min == 1 and profile.standard_count >= 1)
    ):
        return None
    automatic_season_number = title.season_number
    return SingleTitleConfirmationSuggestion(
        title=title,
        metadata_supports_tv=metadata_format in {"TV", "TV_SHORT"},
        proposed_part_type="season",
        proposed_season_number=automatic_season_number or 1,
        proposed_season_label=(
            title.season_label if automatic_season_number is not None else None
        ),
    )


def apply_single_title_confirmation(
    collection: CatalogCollection, *, part_type: str,
    season_number: int | None, season_label: str | None,
    part_number: int | None = None,
) -> CatalogTitle:
    suggestion = single_title_confirmation_suggestion(collection)
    if suggestion is None:
        raise ValueError("Kolekce už nesplňuje podmínky ručního potvrzení jediné části.")
    normalized_type = part_type.strip().casefold()
    if normalized_type not in PART_TYPES:
        raise ValueError("Neplatný typ části.")
    _validate_structural_numbers(normalized_type, season_number, part_number)
    label = (season_label or "").strip()[:50] or None
    if normalized_type in {"season", "part"}:
        if season_number is not None and label is None:
            label = f"S{season_number}"
        if normalized_type == "part" and season_number is None:
            label = None
    else:
        season_number = None
        label = None
    title = suggestion.title
    activate_manual_hierarchy_snapshot(
        title,
        part_type=normalized_type,
        season_number=season_number,
        part_number=part_number if normalized_type in {"part", "cour"} else None,
        season_label=label,
        sort_order=title.effective_sort_order,
        verified_at=utc_now(),
    )
    refresh_collection_state(collection, recalculate=False)
    return title


def _validate_structural_numbers(
    part_type: str, season_number: int | None, part_number: int | None,
) -> None:
    if issue := structural_hierarchy_issue(part_type, season_number, part_number):
        raise ValueError(issue)


def set_manual_title_hierarchy(
    title: CatalogTitle, *, season_number: int | None, season_label: str | None,
    part_type: str | None, sort_order: int | None, hierarchy_verified: bool,
    part_number: int | None = None,
) -> CatalogTitle:
    """Apply the authoritative CatalogTitle hierarchy override used by admin UIs."""
    normalized_label = (season_label or "").strip() or None
    normalized_type = (part_type or "").strip().casefold() or None
    if sort_order is not None and sort_order < 0:
        raise ValueError("Pořadí části nesmí být záporné.")
    if normalized_label and len(normalized_label) > 50:
        raise ValueError("Označení části může mít nejvýše 50 znaků.")
    if normalized_type is not None and normalized_type not in PART_TYPES:
        raise ValueError("Neplatný typ části.")
    manual_values_without_type = any(
        value is not None
        for value in (season_number, normalized_label, part_number, sort_order)
    )
    if normalized_type is None and (
        manual_values_without_type or hierarchy_verified
    ):
        raise ValueError(
            "Pro ruční zařazení zvolte konkrétní typ části."
        )
    has_manual_input = any(
        value is not None
        for value in (
            season_number, normalized_label, part_number, normalized_type, sort_order,
        )
    )
    if has_manual_input and not hierarchy_verified:
        raise ValueError("Ruční hierarchii je nutné potvrdit jako ověřenou.")
    if hierarchy_verified:
        effective_order = title.effective_sort_order
        snapshot_type = normalized_type
        snapshot_season_number = season_number
        snapshot_part_number = part_number
        if snapshot_type not in {"part", "cour"}:
            snapshot_part_number = None
        _validate_structural_numbers(
            snapshot_type, snapshot_season_number, snapshot_part_number,
        )
        if snapshot_type == "part":
            snapshot_label = (
                normalized_label
                or f"S{snapshot_season_number}"
            ) if snapshot_season_number is not None else None
        elif snapshot_type == "season":
            snapshot_label = (
                normalized_label
                or (
                    f"S{snapshot_season_number}"
                    if snapshot_season_number is not None else None
                )
            )
        else:
            snapshot_label = normalized_label
        activate_manual_hierarchy_snapshot(
            title,
            part_type=snapshot_type,
            season_number=snapshot_season_number,
            part_number=snapshot_part_number,
            season_label=snapshot_label,
            sort_order=sort_order if sort_order is not None else effective_order,
            verified_at=utc_now(),
        )
    else:
        clear_manual_hierarchy_snapshot(title)
    if title.collection is not None:
        refresh_collection_state(title.collection)
    return title


def parse_manual_definitions(raw: str) -> list[ManualTitleDefinition]:
    try:
        values = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Definice částí musí být platné JSON pole.") from exc
    if not isinstance(values, list) or not values:
        raise ValueError("Je nutné definovat alespoň jednu část.")
    definitions = []
    for position, value in enumerate(values, 1):
        if not isinstance(value, dict):
            raise ValueError(f"Část {position} není objekt.")
        local_title = str(value.get("local_title") or "").strip()[:200]
        if not local_title:
            raise ValueError(f"Část {position} nemá lokální název.")
        integer_fields = {}
        for field in (
            "title_id", "season_number_manual", "part_number_manual", "episode_start",
            "episode_end", "episode_start_offset", "sort_order",
        ):
            raw_value = value.get(field)
            if field == "part_number_manual" and raw_value in (None, ""):
                # Read old exported technical JSON, but all new output uses the
                # explicit manual field name.
                raw_value = value.get("part_number")
            try:
                integer_fields[field] = None if raw_value in (None, "") else int(raw_value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Část {position}: {field} musí být celé číslo.") from exc
        start, end = integer_fields["episode_start"], integer_fields["episode_end"]
        if (start is None) != (end is None) or (start is not None and (start < 1 or end < start)):
            raise ValueError(f"Část {position} má neplatný rozsah epizod.")
        if integer_fields["episode_start_offset"] is not None and integer_fields["episode_start_offset"] < 0:
            raise ValueError(f"Část {position} má záporný offset.")
        part_type = str(value.get("part_type_manual") or "").strip().casefold() or None
        if part_type and part_type not in PART_TYPES:
            raise ValueError(f"Část {position} má neplatný typ.")
        if part_type is not None:
            try:
                _validate_structural_numbers(
                    part_type, integer_fields["season_number_manual"],
                    integer_fields["part_number_manual"],
                )
            except ValueError as exc:
                raise ValueError(f"Část {position}: {exc}") from exc
        elif any((
            integer_fields["season_number_manual"] is not None,
            integer_fields["part_number_manual"] is not None,
            bool(_optional_text(value.get("season_label_manual"), 50)),
        )):
            raise ValueError(
                f"Část {position}: ruční hodnoty vyžadují konkrétní typ části."
            )
        mode = str(value.get("numbering_mode") or "unknown").strip().casefold()
        if mode not in ALLOWED_NUMBERING_MODES:
            raise ValueError(f"Část {position} má neplatný režim číslování.")
        pattern = str(value.get("filename_pattern") or "").strip() or None
        if pattern:
            compile_manual_split_pattern(pattern)
        try:
            video_ids = tuple(int(item) for item in (value.get("video_ids") or []))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Část {position}: video_ids musí být seznam ID.") from exc
        definitions.append(ManualTitleDefinition(
            title_id=integer_fields["title_id"], local_title=local_title,
            manual_display_title=_optional_text(value.get("manual_display_title"), 200),
            season_number_manual=integer_fields["season_number_manual"],
            season_label_manual=_optional_text(value.get("season_label_manual"), 50),
            part_number_manual=integer_fields["part_number_manual"],
            part_type_manual=part_type,
            episode_start=start, episode_end=end,
            episode_start_offset=integer_fields["episode_start_offset"],
            numbering_mode=mode, sort_order=integer_fields["sort_order"] or position,
            filename_pattern=pattern,
            video_ids=video_ids,
        ))
    return definitions


def parse_simple_definitions(
    rows: list[Mapping[str, str]],
) -> list[ManualTitleDefinition]:
    values = []
    for row in rows:
        value = {field: str(row.get(field) or "").strip() for field in SIMPLE_DEFINITION_FIELDS}
        if not value["part_number_manual"] and row.get("part_number"):
            value["part_number_manual"] = str(row.get("part_number") or "").strip()
        is_blank_new_row = (
            not value["title_id"]
            and not value["local_title"]
            and not any(
                value[field] for field in SIMPLE_DEFINITION_FIELDS
                if field not in {"title_id", "local_title", "numbering_mode"}
            )
            and value["numbering_mode"] in {"", "unknown"}
        )
        if is_blank_new_row:
            continue
        value["numbering_mode"] = value["numbering_mode"] or "unknown"
        value["video_ids"] = [
            token for token in re.split(r"[\s,]+", value["video_ids"])
            if token
        ]
        values.append(value)
    return parse_manual_definitions(json.dumps(values, ensure_ascii=False))


def simple_definition_rows(collection: CatalogCollection) -> list[dict[str, str | int | None]]:
    rows = []
    for title in sorted(collection.titles, key=lambda item: item.effective_sort_order):
        definition = definition_from_title(title)
        rows.append({
            "title_id": title.id,
            "local_title": title.local_title,
            "manual_display_title": title.manual_display_title,
            "season_number_manual": title.season_number_manual,
            "season_label_manual": title.season_label_manual,
            "part_number_manual": title.part_number_manual,
            "part_type_manual": title.part_type_manual,
            "episode_start": title.episode_start,
            "episode_end": title.episode_end,
            "episode_start_offset": title.episode_start_offset,
            "numbering_mode": title.numbering_mode,
            "sort_order": title.effective_sort_order,
            "filename_pattern": title.episode_filename_pattern,
            "video_ids": ", ".join(map(str, definition.video_ids)),
        })
    rows.append({field: "" for field in SIMPLE_DEFINITION_FIELDS})
    return rows


def _optional_text(value, limit: int) -> str | None:
    return str(value or "").strip()[:limit] or None


def _validate_manual_split_definition_targets(
    collection: CatalogCollection,
    definitions: list[ManualTitleDefinition],
) -> None:
    existing_ids = {
        title.id for title in collection.titles if title.id is not None
    }
    referenced_ids = {
        definition.title_id
        for definition in definitions
        if definition.title_id is not None
    }
    if referenced_ids - existing_ids:
        raise ValueError("Definice odkazuje na cizí nebo neexistující část.")
    omitted_active_ids = {
        title.id
        for title in collection.titles
        if title.id is not None
        and has_persisted_manual_split_selector(title)
        and title.id not in referenced_ids
    }
    if omitted_active_ids:
        raise ValueError(
            "Aktivní část ručního rozdělení nelze z definice vynechat; "
            "odstraňte ji samostatnou potvrzenou akcí."
        )


def preview_assignments(
    videos: list[Video],
    definitions: list[ManualTitleDefinition],
    *,
    collection: CatalogCollection | None = None,
) -> AssignmentPreview:
    if collection is not None:
        _validate_manual_split_definition_targets(collection, definitions)
    return evaluate_manual_split_assignment(videos, definitions)


def _load_collection_for_assignment(session: Session, collection_id: int) -> CatalogCollection:
    collection = session.scalar(select(CatalogCollection).options(
        selectinload(CatalogCollection.titles).selectinload(CatalogTitle.videos),
        selectinload(CatalogCollection.titles).selectinload(CatalogTitle.metadata_record),
        selectinload(CatalogCollection.titles).selectinload(CatalogTitle.external_links),
        selectinload(CatalogCollection.titles).selectinload(CatalogTitle.metadata_candidates),
        selectinload(CatalogCollection.titles).selectinload(CatalogTitle.artwork),
        selectinload(CatalogCollection.videos),
    ).where(CatalogCollection.id == collection_id))
    if collection is None:
        raise ValueError("Kolekce nebyla nalezena.")
    return collection


def _selected_videos(collection: CatalogCollection, video_ids: list[int]) -> list[Video]:
    selected_ids = {int(video_id) for video_id in video_ids}
    if not selected_ids:
        raise ValueError("Vyberte alespoň jedno video.")
    selected = [video for video in collection.videos if video.id in selected_ids]
    if len(selected) != len(selected_ids):
        raise ValueError("Výběr obsahuje cizí nebo neexistující video.")
    return selected


def refresh_collection_state(
    collection: CatalogCollection, *, recalculate: bool = True,
) -> None:
    finalize_hierarchy_write([collection], recalculate=recalculate)


def classify_videos_in_place(
    session: Session, collection_id: int, video_ids: list[int], content_type: str,
) -> list[Video]:
    normalized_type = content_type.strip().casefold() or None
    if normalized_type is not None and normalized_type not in VIDEO_CONTENT_TYPES:
        raise ValueError("Neplatný typ doplňkového obsahu.")
    collection = _load_collection_for_assignment(session, collection_id)
    selected = _selected_videos(collection, video_ids)
    for video in selected:
        video.content_type_manual = normalized_type
    session.flush()
    refresh_collection_state(collection)
    return selected


def confirm_duplicate_videos(
    session: Session, collection_id: int, video_ids: list[int], primary_video_id: int,
) -> list[Video]:
    collection = _load_collection_for_assignment(session, collection_id)
    selected = _selected_videos(collection, video_ids)
    primary = next((video for video in selected if video.id == primary_video_id), None)
    if primary is None:
        raise ValueError("Primární kopie musí být součástí potvrzované skupiny.")
    clear_duplicate_group(selected)
    session.flush()
    set_duplicate_group_primary(selected, primary)
    session.flush()
    refresh_collection_state(collection)
    return selected


def confirm_duplicate_groups(
    session: Session, collection_id: int,
    assignments: list[tuple[list[int], int]],
) -> list[Video]:
    if not assignments:
        raise ValueError("Není vybraná žádná skupina duplicit.")
    collection = _load_collection_for_assignment(session, collection_id)
    changed: list[Video] = []
    used_ids: set[int] = set()
    for video_ids, primary_video_id in assignments:
        selected = _selected_videos(collection, video_ids)
        selected_ids = {video.id for video in selected}
        if used_ids & selected_ids:
            raise ValueError("Jedno video nemůže být v několika skupinách duplicit.")
        primary = next((video for video in selected if video.id == primary_video_id), None)
        if primary is None:
            raise ValueError("Primární kopie musí být součástí potvrzované skupiny.")
        clear_duplicate_group(selected)
        session.flush()
        set_duplicate_group_primary(selected, primary)
        used_ids.update(selected_ids)
        changed.extend(selected)
    session.flush()
    refresh_collection_state(collection)
    return changed


def clear_confirmed_duplicate_videos(
    session: Session, collection_id: int, video_ids: list[int],
) -> list[Video]:
    collection = _load_collection_for_assignment(session, collection_id)
    selected = _selected_videos(collection, video_ids)
    if len(selected) < 2 and not any(video.duplicate_primary_missing for video in selected):
        raise ValueError("Pro zrušení duplicity je nutné vybrat celou skupinu.")
    clear_duplicate_group(selected)
    session.flush()
    refresh_collection_state(collection)
    return selected


def move_videos_to_title(
    session: Session, collection_id: int, video_ids: list[int], target_title_id: int,
) -> CatalogTitle:
    collection = _load_collection_for_assignment(session, collection_id)
    selected = _selected_videos(collection, video_ids)
    target = next((title for title in collection.titles if title.id == target_title_id), None)
    if target is None:
        raise ValueError("Cílová část neexistuje v této kolekci.")
    for video in selected:
        video.catalog_title = target
        video.catalog_collection = collection
    replace_explicit_video_selector_authority(selected, target)
    session.flush()
    refresh_collection_state(collection)
    return target


def create_title_from_videos(
    session: Session, collection_id: int, video_ids: list[int], *,
    local_title: str, part_type: str, season_number: int | None = None,
    season_label: str | None = None, part_number: int | None = None,
) -> CatalogTitle:
    name = local_title.strip()[:200]
    normalized_type = part_type.strip().casefold()
    if not name:
        raise ValueError("Nová část musí mít název.")
    if normalized_type not in PART_TYPES:
        raise ValueError("Neplatný typ části.")
    _validate_structural_numbers(normalized_type, season_number, part_number)
    normalized_label = (season_label or "").strip()[:50] or (
        f"S{season_number}" if season_number is not None else None
    )
    if normalized_type == "part" and season_number is None:
        normalized_label = None
    collection = _load_collection_for_assignment(session, collection_id)
    selected = _selected_videos(collection, video_ids)
    position = max((title.effective_sort_order for title in collection.titles), default=0) + 1
    virtual_path = f"{collection.relative_root_path}/.catalog-part-{position}"
    suffix = 1
    while session.scalar(select(CatalogTitle.id).where(
        CatalogTitle.relative_root_path == virtual_path
    )):
        suffix += 1
        virtual_path = f"{collection.relative_root_path}/.catalog-part-{position}-{suffix}"
    title = CatalogTitle(
        collection=collection, local_title=name, normalized_local_title=normalize_title(name),
        relative_root_path=virtual_path, numbering_mode="unknown",
    )
    session.add(title)
    session.flush()
    activate_manual_hierarchy_snapshot(
        title,
        part_type=normalized_type,
        season_number=season_number,
        part_number=part_number if normalized_type in {"part", "cour"} else None,
        season_label=normalized_label,
        sort_order=position,
        verified_at=utc_now(),
    )
    replace_explicit_video_selector_authority(selected, title)
    for video in selected:
        if normalized_type in VIDEO_CONTENT_TYPES:
            video.content_type_manual = video.content_type_manual or normalized_type
        video.catalog_title = title
        video.catalog_collection = collection
    session.flush()
    refresh_collection_state(collection)
    return title


def merge_title_into(
    session: Session, collection_id: int, source_title_id: int, target_title_id: int,
) -> CatalogTitle:
    if source_title_id == target_title_id:
        raise ValueError("Zdrojová a cílová část musí být odlišná.")
    collection = _load_collection_for_assignment(session, collection_id)
    source = next((title for title in collection.titles if title.id == source_title_id), None)
    if source is None:
        raise ValueError("Zdrojová část neexistuje v této kolekci.")
    if not source.videos:
        raise ValueError("Zdrojová část neobsahuje žádná videa.")
    return move_videos_to_title(
        session, collection_id, [video.id for video in source.videos], target_title_id
    )


def delete_empty_local_title(
    session: Session, collection_id: int, title_id: int, *,
    remove_from_manual_split: bool = False,
) -> bool:
    title = session.scalar(select(CatalogTitle).options(
        selectinload(CatalogTitle.external_links),
        selectinload(CatalogTitle.metadata_record),
        selectinload(CatalogTitle.metadata_candidates),
        selectinload(CatalogTitle.artwork),
    ).where(
        CatalogTitle.id == title_id,
        CatalogTitle.catalog_collection_id == collection_id,
    ))
    if title is None:
        raise ValueError("Část neexistuje v této kolekci.")
    is_manual_split_entry = has_persisted_manual_split_selector(title)
    if is_manual_split_entry and not remove_from_manual_split:
        raise ValueError(
            "Část je součástí ruční definice rozdělení; její odstranění z definice "
            "je nutné explicitně potvrdit."
        )
    video_count = session.scalar(select(func.count(Video.id)).where(
        Video.catalog_title_id == title_id
    )) or 0
    if video_count:
        raise ValueError(
            "Část už není prázdná; obsahuje video a nebyla odstraněna."
        )
    # Explicitní vyprázdnění zachovává vlastní aplikační cleanup vedle FK cascade.
    title.external_links.clear()
    title.metadata_candidates.clear()
    title.artwork.clear()
    title.metadata_record = None
    session.flush()
    deleted = session.execute(delete(CatalogTitle).where(
        CatalogTitle.id == title_id,
        ~select(Video.id).where(Video.catalog_title_id == CatalogTitle.id).exists(),
    ).execution_options(synchronize_session=False))
    if deleted.rowcount != 1:
        raise ValueError(
            "Část už není prázdná; mezitím do ní přibylo video a nebyla odstraněna."
        )
    session.execute(delete(ManualSplitRuleVideo).where(
        ManualSplitRuleVideo.catalog_title_id == title_id
    ))
    session.flush()
    collection = session.get(CatalogCollection, collection_id)
    if collection is not None:
        session.expire(collection, ["titles"])
        refresh_collection_state(collection)
    return is_manual_split_entry


def separate_nonstandard_videos(
    session: Session, collection_id: int, video_ids: list[int], *,
    local_title: str, part_type: str, season_number: int | None = None,
    part_number: int | None = None,
) -> CatalogTitle:
    """Logicky oddělí vybraná nestandardní videa bez změny jejich cesty."""
    collection = _load_collection_for_assignment(session, collection_id)
    selected = _selected_videos(collection, video_ids)
    if any(not effective_video_numbering(video).is_nonstandard for video in selected):
        raise ValueError("Oddělit lze touto akcí pouze rozpoznaný nestandardní obsah.")
    return create_title_from_videos(
        session, collection_id, video_ids, local_title=local_title,
        part_type=part_type, season_number=season_number, part_number=part_number,
    )


def apply_manual_split(
    session: Session, collection_id: int, definitions: list[ManualTitleDefinition],
    *, confirm_conflicts: bool = False,
) -> AssignmentPreview:
    collection = session.scalar(select(CatalogCollection).options(
        selectinload(CatalogCollection.titles).selectinload(
            CatalogTitle.manual_split_rule_videos
        ),
        selectinload(CatalogCollection.videos),
    ).where(CatalogCollection.id == collection_id))
    if collection is None:
        raise ValueError("Kolekce nebyla nalezena.")
    preview = preview_assignments(
        collection.videos,
        definitions,
        collection=collection,
    )
    if preview.conflicts and not confirm_conflicts:
        raise ValueError("Rozsahy nebo pravidla se překrývají; je nutné explicitní potvrzení.")
    existing = {title.id: title for title in collection.titles}
    resolved: list[CatalogTitle] = []
    now = utc_now()
    for position, definition in enumerate(definitions, 1):
        title = existing.get(definition.title_id) if definition.title_id else None
        if definition.title_id and title is None:
            raise ValueError("Definice odkazuje na cizí nebo neexistující část.")
        if title is None:
            virtual_path = f"{collection.relative_root_path}/.catalog-part-{position}"
            suffix = 1
            while session.scalar(select(CatalogTitle.id).where(CatalogTitle.relative_root_path == virtual_path)):
                suffix += 1
                virtual_path = f"{collection.relative_root_path}/.catalog-part-{position}-{suffix}"
            title = CatalogTitle(
                collection=collection, local_title=definition.local_title,
                normalized_local_title=normalize_title(definition.local_title),
                relative_root_path=virtual_path,
            )
            session.add(title)
        title.local_title = definition.local_title
        title.normalized_local_title = normalize_title(definition.local_title)
        title.manual_display_title = definition.manual_display_title
        if definition.part_type_manual is not None:
            snapshot_part_number = definition.part_number_manual
            if definition.part_type_manual not in {"part", "cour"}:
                snapshot_part_number = None
            activate_manual_hierarchy_snapshot(
                title,
                part_type=definition.part_type_manual,
                season_number=definition.season_number_manual,
                part_number=snapshot_part_number,
                season_label=definition.season_label_manual,
                sort_order=definition.sort_order,
                verified_at=now,
            )
        title.episode_start = definition.episode_start
        title.episode_end = definition.episode_end
        title.episode_start_offset = definition.episode_start_offset
        title.numbering_mode = definition.numbering_mode
        title.episode_filename_pattern = definition.filename_pattern
        resolved.append(title)
    session.flush()
    synchronize_manual_split_authority(
        definitions,
        resolved,
        list(collection.videos),
    )
    session.flush()
    apply_manual_split_decisions(
        preview,
        collection,
        catalog_titles=resolved,
    )
    session.flush()
    refresh_collection_state(collection)
    return preview


def definitions_as_json(collection: CatalogCollection) -> str:
    return definitions_to_json([
        definition_from_title(title)
        for title in sorted(
            collection.titles,
            key=lambda item: item.effective_sort_order,
        )
    ])


def definitions_to_json(definitions: list[ManualTitleDefinition]) -> str:
    return json.dumps(
        [
            {**definition.__dict__, "video_ids": list(definition.video_ids)}
            for definition in definitions
        ],
        ensure_ascii=False,
        indent=2,
    )
