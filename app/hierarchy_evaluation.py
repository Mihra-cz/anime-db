from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .catalog import detect_episode_number
from .hierarchy_types import PART_TYPES
from .hierarchy_provenance import (
    RELATED_NAMED_CHILD_REVIEW_REASON,
    SUPPLEMENTARY_NAMED_CHILD_REVIEW_REASON,
    NamedChildProvenanceKind,
    derive_collection_path_provenance,
)
from .manual_split import (
    ManualSplitDecisionKind,
    evaluate_persisted_manual_split,
)
from .models import CatalogCollection, CatalogTitle, Video, utc_now
from .numbering import (
    confirmed_duplicate_groups,
    effective_video_numbering,
    is_confirmed_duplicate,
    is_nonprimary_duplicate_video,
    recalculate_collection_numbering,
    summarize_title_numbering,
    unresolved_duplicate_groups,
)
from .structural_inference import (
    GENERIC_TITLE_REVIEW_REASON,
    LONG_FLAT_SEQUENCE_REVIEW_REASON,
    apply_automatic_structural_inference,
    automatic_flat_sequence_notice,
    direct_root_episode_profile,
    has_long_flat_sequence_requiring_review,
)


CONFIRMED_DUPLICATES_REVIEW_REASON = (
    "Potvrzené duplicitní soubory vyžadují vyřešení."
)
MISSING_DUPLICATE_PRIMARY_REVIEW_REASON = (
    "Primární video potvrzené duplicity chybí; vztah vyžaduje novou ruční kontrolu."
)
FILENAME_SEASON_CONFLICT_REVIEW_REASON = (
    "Season ve filename je v konfliktu s automaticky odvozenou season složkou."
)
UNNUMBERED_SUPPLEMENTARY_REVIEW_REASON = (
    "Explicitně označený doplňkový obsah nemá bezpečně určené canonical číslování."
)
MISSING_PART_NUMBER_REVIEW_REASON = (
    "Část typu Part nemá bezpečně určené číslo Part."
)
NONSTANDARD_NUMBERING_REVIEW_REASON = (
    "Nestandardní číslování vyžaduje ruční zařazení."
)
NUMBERING_REVIEW_SUMMARY = (
    "Číslování nebo nezařazený obsah stále vyžaduje kontrolu."
)
UNASSIGNED_VIDEO_REVIEW_REASON = (
    "Nové nebo nezařazené video vyžaduje kontrolu."
)
MANUAL_SPLIT_CONFLICT_REVIEW_REASON = (
    "Video odpovídá více pravidlům ručního rozdělení."
)
MANUAL_SPLIT_UNMATCHED_REVIEW_REASON = (
    "Video, které vyžaduje ruční rozdělení, neodpovídá žádnému pravidlu."
)


class HierarchyIssueCode(StrEnum):
    MANUAL_SPLIT_CONFLICT = "manual_split_conflict"
    MANUAL_SPLIT_UNMATCHED = "manual_split_unmatched"
    UNASSIGNED_VIDEO = "unassigned_video"
    FILENAME_SEASON_CONFLICT = "filename_season_conflict"
    SUPPLEMENTARY_WITHOUT_NUMBER = "supplementary_without_number"
    NONSTANDARD_NUMBERING = "nonstandard_numbering"
    UNKNOWN_OR_MISSING_NUMBERING = "unknown_or_missing_numbering"
    NUMBERING_GAP = "numbering_gap"
    CANONICAL_DUPLICATE = "canonical_duplicate"
    CONFIRMED_DUPLICATE = "confirmed_duplicate"
    DUPLICATE_PRIMARY_MISSING = "duplicate_primary_missing"
    LONG_FLAT_SERIES = "long_flat_series"
    SOFT_LONG_FLAT_SERIES = "soft_long_flat_series"
    GENERIC_STRUCTURAL_TYPE = "generic_structural_type"
    MISSING_PART_NUMBER = "missing_part_number"
    INCOMPLETE_MANUAL_SNAPSHOT = "incomplete_manual_snapshot"
    RELATED_NAMED_CHILD = "related_named_child"
    SUPPLEMENTARY_NAMED_CHILD = "supplementary_named_child"
    LEGACY_UNLOCALIZED_REVIEW_STATE = "legacy_unlocalized_review_state"


class HierarchyIssueScope(StrEnum):
    COLLECTION = "collection"
    CATALOG_TITLE = "catalog_title"
    VIDEO = "video"


@dataclass(frozen=True)
class HierarchyIssue:
    """One current hierarchy fact with stable identity and natural scope."""

    code: HierarchyIssueCode
    blocking: bool
    scope: HierarchyIssueScope
    message: str
    catalog_title: CatalogTitle | None = None
    videos: tuple[Video, ...] = ()
    related_catalog_titles: tuple[CatalogTitle, ...] = ()

    @property
    def catalog_title_id(self) -> int | None:
        return self.catalog_title.id if self.catalog_title is not None else None

    @property
    def video_ids(self) -> tuple[int, ...]:
        return tuple(video.id for video in self.videos if video.id is not None)

    @property
    def related_catalog_title_ids(self) -> tuple[int, ...]:
        return tuple(
            title.id for title in self.related_catalog_titles
            if title.id is not None
        )


@dataclass(frozen=True)
class HierarchyEvaluationResult:
    """Complete read-only hierarchy evaluation for one current collection state."""

    issues: tuple[HierarchyIssue, ...]
    status: str
    primary_note: str | None

    @property
    def blocking_issues(self) -> tuple[HierarchyIssue, ...]:
        return tuple(issue for issue in self.issues if issue.blocking)

    @property
    def soft_warnings(self) -> tuple[HierarchyIssue, ...]:
        return tuple(issue for issue in self.issues if not issue.blocking)


def structural_hierarchy_issue(
    part_type: str | None,
    season_number: int | None,
    part_number: int | None,
) -> str | None:
    """Validate one concrete authoritative structural identity."""
    if part_type not in PART_TYPES:
        return "Pro ruční zařazení zvolte konkrétní typ části."
    if season_number is not None and season_number <= 0:
        return "Číslo sezóny musí být kladné."
    if part_number is not None and part_number <= 0:
        return "Číslo Part musí být kladné."
    if part_type == "part" and part_number is None:
        return "Pro typ Part potvrďte číslo Part."
    if part_type == "season" and part_number is not None:
        return "Samostatná sezóna nesmí mít číslo Part."
    if part_type not in {"part", "cour"} and part_number is not None:
        return "Číslo Part lze uložit pouze pro typ Part."
    return None


def manual_hierarchy_snapshot_issue(title: CatalogTitle) -> str | None:
    """Validate persisted manual authority without repairing historical data."""
    if not title.hierarchy_manual_override:
        return "Chybí autoritativní ruční hierarchy override."
    return structural_hierarchy_issue(
        title.part_type_manual,
        title.season_number_manual,
        title.part_number_manual,
    )


def manual_hierarchy_snapshot_is_complete(title: CatalogTitle) -> bool:
    return manual_hierarchy_snapshot_issue(title) is None


def catalog_title_hierarchy_is_verified(title: CatalogTitle) -> bool:
    return manual_hierarchy_snapshot_is_complete(title)


def manual_hierarchy_resolves_ambiguity(collection: CatalogCollection) -> bool:
    return bool(collection.titles) and all(
        manual_hierarchy_snapshot_is_complete(title)
        for title in collection.titles
    )


def _video_title(
    video: Video,
    titles_by_id: dict[int, CatalogTitle],
) -> CatalogTitle | None:
    if video.catalog_title is not None:
        return video.catalog_title
    if video.catalog_title_id is not None:
        return titles_by_id.get(video.catalog_title_id)
    return None


def _filename_season_conflicts_with_title(
    video: Video,
    title: CatalogTitle | None,
) -> bool:
    detection = detect_episode_number(video.filename)
    return bool(
        detection.season_hint is not None
        and title is not None
        and title.effective_season_number is not None
        and detection.season_hint != title.effective_season_number
        and not title.hierarchy_manual_override
    )


def _has_unnumbered_explicit_supplementary(video: Video) -> bool:
    detection = detect_episode_number(video.filename)
    return bool(
        detection.is_supplementary
        and detection.supplementary_number is None
        and video.episode_number_manual_override is None
    )


def _common_catalog_title(videos: tuple[Video, ...]) -> CatalogTitle | None:
    titles = {
        video.catalog_title
        for video in videos
        if video.catalog_title is not None
    }
    return next(iter(titles)) if len(titles) == 1 else None


def derive_hierarchy_status(
    collection: CatalogCollection,
    issues: tuple[HierarchyIssue, ...],
) -> str:
    blocking = tuple(issue for issue in issues if issue.blocking)
    if blocking:
        if any(
            issue.code == HierarchyIssueCode.MANUAL_SPLIT_CONFLICT
            for issue in blocking
        ):
            return "conflict"
        if (
            collection.hierarchy_status == "conflict"
            and all(
                issue.code == HierarchyIssueCode.LEGACY_UNLOCALIZED_REVIEW_STATE
                for issue in blocking
            )
        ):
            return "conflict"
        return "review_required"
    if manual_hierarchy_resolves_ambiguity(collection):
        return "verified"
    return "automatic"


_NUMBERING_CODES = {
    HierarchyIssueCode.NONSTANDARD_NUMBERING,
    HierarchyIssueCode.UNKNOWN_OR_MISSING_NUMBERING,
    HierarchyIssueCode.NUMBERING_GAP,
    HierarchyIssueCode.CANONICAL_DUPLICATE,
}


def hierarchy_primary_note(
    issues: tuple[HierarchyIssue, ...],
    *,
    summarize_numbering: bool = True,
) -> str | None:
    """Choose one compatibility summary from codes, never from message matching."""
    blocking = tuple(issue for issue in issues if issue.blocking)
    codes = {issue.code for issue in blocking}
    if HierarchyIssueCode.MANUAL_SPLIT_CONFLICT in codes:
        return MANUAL_SPLIT_CONFLICT_REVIEW_REASON
    if HierarchyIssueCode.MANUAL_SPLIT_UNMATCHED in codes:
        return MANUAL_SPLIT_UNMATCHED_REVIEW_REASON
    if HierarchyIssueCode.UNASSIGNED_VIDEO in codes:
        return UNASSIGNED_VIDEO_REVIEW_REASON
    if summarize_numbering and codes & _NUMBERING_CODES:
        return NUMBERING_REVIEW_SUMMARY
    priorities = (
        (HierarchyIssueCode.FILENAME_SEASON_CONFLICT, FILENAME_SEASON_CONFLICT_REVIEW_REASON),
        (HierarchyIssueCode.SUPPLEMENTARY_WITHOUT_NUMBER, UNNUMBERED_SUPPLEMENTARY_REVIEW_REASON),
        (HierarchyIssueCode.NONSTANDARD_NUMBERING, NONSTANDARD_NUMBERING_REVIEW_REASON),
        (HierarchyIssueCode.DUPLICATE_PRIMARY_MISSING, MISSING_DUPLICATE_PRIMARY_REVIEW_REASON),
        (HierarchyIssueCode.CONFIRMED_DUPLICATE, CONFIRMED_DUPLICATES_REVIEW_REASON),
        (HierarchyIssueCode.LONG_FLAT_SERIES, LONG_FLAT_SEQUENCE_REVIEW_REASON),
        (HierarchyIssueCode.RELATED_NAMED_CHILD, RELATED_NAMED_CHILD_REVIEW_REASON),
        (
            HierarchyIssueCode.SUPPLEMENTARY_NAMED_CHILD,
            SUPPLEMENTARY_NAMED_CHILD_REVIEW_REASON,
        ),
        (HierarchyIssueCode.GENERIC_STRUCTURAL_TYPE, GENERIC_TITLE_REVIEW_REASON),
        (HierarchyIssueCode.MISSING_PART_NUMBER, MISSING_PART_NUMBER_REVIEW_REASON),
    )
    for code, message in priorities:
        if code in codes:
            return message
    return blocking[0].message if blocking else None


def evaluate_collection_hierarchy(
    collection: CatalogCollection,
    videos: list[Video] | None = None,
    *,
    include_legacy_fallback: bool = True,
    include_unassigned: bool = True,
    include_complete_numbering: bool = True,
) -> HierarchyEvaluationResult:
    """Evaluate current final data without mutating hierarchy or numbering."""
    titles = list(collection.titles)
    all_videos = list(collection.videos if videos is None else videos)
    titles_by_id = {
        title.id: title for title in titles if title.id is not None
    }
    videos_by_title: dict[int, list[Video]] = {
        title.id: [] for title in titles if title.id is not None
    }
    transient_title_videos: dict[int, list[Video]] = {}
    for video in all_videos:
        title = _video_title(video, titles_by_id)
        if title is None:
            continue
        if title.id is not None:
            videos_by_title.setdefault(title.id, []).append(video)
        else:
            transient_title_videos.setdefault(id(title), []).append(video)

    issues: list[HierarchyIssue] = []
    path_provenance = derive_collection_path_provenance(collection, all_videos)
    provenance_by_title: dict[int, list[NamedChildProvenanceKind]] = {}
    for item in path_provenance:
        provenance_by_title.setdefault(id(item.catalog_title), []).append(item.kind)

    def add_issue(
        code: HierarchyIssueCode,
        message: str,
        scope: HierarchyIssueScope,
        *,
        blocking: bool,
        title: CatalogTitle | None = None,
        target_videos: tuple[Video, ...] = (),
        related_titles: tuple[CatalogTitle, ...] = (),
    ) -> None:
        issues.append(HierarchyIssue(
            code=code,
            blocking=blocking,
            scope=scope,
            message=message,
            catalog_title=title,
            videos=target_videos,
            related_catalog_titles=related_titles,
        ))

    manual_split = evaluate_persisted_manual_split(collection, all_videos)
    manual_problem_videos: set[Video] = set()
    for decision in manual_split.decisions:
        if decision.kind == ManualSplitDecisionKind.CONFLICT:
            manual_problem_videos.add(decision.video)
            add_issue(
                HierarchyIssueCode.MANUAL_SPLIT_CONFLICT,
                MANUAL_SPLIT_CONFLICT_REVIEW_REASON,
                HierarchyIssueScope.VIDEO,
                blocking=True,
                target_videos=(decision.video,),
                related_titles=decision.matching_catalog_titles,
            )
        elif decision.kind == ManualSplitDecisionKind.UNMATCHED:
            manual_problem_videos.add(decision.video)
            add_issue(
                HierarchyIssueCode.MANUAL_SPLIT_UNMATCHED,
                MANUAL_SPLIT_UNMATCHED_REVIEW_REASON,
                HierarchyIssueScope.VIDEO,
                blocking=True,
                target_videos=(decision.video,),
            )

    for video in all_videos:
        title = _video_title(video, titles_by_id)
        target = (video,)
        if (
            include_unassigned
            and title is None
            and video not in manual_problem_videos
        ):
            add_issue(
                HierarchyIssueCode.UNASSIGNED_VIDEO,
                "Video není přiřazeno ke konkrétní části.",
                HierarchyIssueScope.VIDEO,
                blocking=True,
                target_videos=target,
            )
        if _filename_season_conflicts_with_title(video, title):
            detection = detect_episode_number(video.filename)
            add_issue(
                HierarchyIssueCode.FILENAME_SEASON_CONFLICT,
                (
                    f"Filename navrhuje S{detection.season_hint}, ale část je "
                    f"automaticky zařazena jako S{title.effective_season_number}. "
                    f"{FILENAME_SEASON_CONFLICT_REVIEW_REASON}"
                ),
                HierarchyIssueScope.VIDEO,
                blocking=True,
                title=title,
                target_videos=target,
            )
        if _has_unnumbered_explicit_supplementary(video):
            add_issue(
                HierarchyIssueCode.SUPPLEMENTARY_WITHOUT_NUMBER,
                UNNUMBERED_SUPPLEMENTARY_REVIEW_REASON,
                HierarchyIssueScope.VIDEO,
                blocking=True,
                title=title,
                target_videos=target,
            )
        if video.duplicate_primary_missing:
            add_issue(
                HierarchyIssueCode.DUPLICATE_PRIMARY_MISSING,
                MISSING_DUPLICATE_PRIMARY_REVIEW_REASON,
                HierarchyIssueScope.VIDEO,
                blocking=True,
                title=title,
                target_videos=target,
            )

    for title in titles:
        title_videos = (
            videos_by_title.get(title.id, [])
            if title.id is not None
            else transient_title_videos.get(id(title), [])
        )
        summary = summarize_title_numbering(title_videos, title)

        if not summary.supplemental:
            for video in title_videos:
                if is_nonprimary_duplicate_video(video):
                    continue
                state = effective_video_numbering(video, title)
                if state.is_nonstandard:
                    add_issue(
                        HierarchyIssueCode.NONSTANDARD_NUMBERING,
                        (
                            f"Detekovaná hodnota {state.detection.display_value} je "
                            f"nestandardní. {NONSTANDARD_NUMBERING_REVIEW_REASON}"
                        ),
                        HierarchyIssueScope.VIDEO,
                        blocking=True,
                        title=title,
                        target_videos=(video,),
                    )
                elif include_complete_numbering and (
                    state.is_unknown
                    or state.is_standard and state.season_episode_number is None
                ):
                    message = (
                        "Číslo epizody nelze bezpečně určit."
                        if state.is_unknown
                        else "Standardní epizoda nemá určené canonical číslo."
                    )
                    add_issue(
                        HierarchyIssueCode.UNKNOWN_OR_MISSING_NUMBERING,
                        message,
                        HierarchyIssueScope.VIDEO,
                        blocking=True,
                        title=title,
                        target_videos=(video,),
                    )

            if include_complete_numbering and summary.gaps:
                missing = ", ".join(f"E{number}" for number in summary.gaps)
                add_issue(
                    HierarchyIssueCode.NUMBERING_GAP,
                    f"V canonical řadě chybí {missing}.",
                    HierarchyIssueScope.CATALOG_TITLE,
                    blocking=True,
                    title=title,
                )

            if include_complete_numbering:
                represented_duplicate_numbers: set[int] = set()
                for group in unresolved_duplicate_groups(title_videos):
                    group_title = _common_catalog_title(group.videos) or title
                    represented_duplicate_numbers.add(group.episode_number)
                    add_issue(
                        HierarchyIssueCode.CANONICAL_DUPLICATE,
                        (
                            "Více videí má stejné nepotvrzené canonical číslo "
                            f"{group.display_label}."
                        ),
                        HierarchyIssueScope.VIDEO,
                        blocking=True,
                        title=group_title,
                        target_videos=group.videos,
                    )
                unresolved_without_group = tuple(
                    number for number in summary.duplicate_numbers
                    if number not in represented_duplicate_numbers
                )
                if unresolved_without_group:
                    labels = ", ".join(
                        f"E{number}" for number in unresolved_without_group
                    )
                    add_issue(
                        HierarchyIssueCode.CANONICAL_DUPLICATE,
                        f"Více videí sdílí nepotvrzená canonical čísla {labels}.",
                        HierarchyIssueScope.CATALOG_TITLE,
                        blocking=True,
                        title=title,
                    )

        if has_long_flat_sequence_requiring_review(title, title_videos):
            profile = direct_root_episode_profile(title_videos)
            episode_range = (
                f"E{profile.episode_min}–E{profile.episode_max}"
                if profile.episode_min is not None and profile.episode_max is not None
                else "neznámý rozsah"
            )
            add_issue(
                HierarchyIssueCode.LONG_FLAT_SERIES,
                (
                    f"Neobvykle dlouhá souvislá řada: {episode_range}. "
                    "Ověřte, zda nejde o více sezón nebo částí."
                ),
                HierarchyIssueScope.CATALOG_TITLE,
                blocking=True,
                title=title,
            )
        elif notice := automatic_flat_sequence_notice(title, title_videos):
            add_issue(
                HierarchyIssueCode.SOFT_LONG_FLAT_SERIES,
                notice,
                HierarchyIssueScope.CATALOG_TITLE,
                blocking=False,
                title=title,
            )

        for provenance_kind in provenance_by_title.get(id(title), []):
            if provenance_kind == NamedChildProvenanceKind.RELATED_NAMED_CHILD:
                add_issue(
                    HierarchyIssueCode.RELATED_NAMED_CHILD,
                    RELATED_NAMED_CHILD_REVIEW_REASON,
                    HierarchyIssueScope.CATALOG_TITLE,
                    blocking=True,
                    title=title,
                )
            elif (
                provenance_kind
                == NamedChildProvenanceKind.SUPPLEMENTARY_NAMED_CHILD
            ):
                add_issue(
                    HierarchyIssueCode.SUPPLEMENTARY_NAMED_CHILD,
                    SUPPLEMENTARY_NAMED_CHILD_REVIEW_REASON,
                    HierarchyIssueScope.CATALOG_TITLE,
                    blocking=True,
                    title=title,
                )

        if title.effective_part_type == "title":
            add_issue(
                HierarchyIssueCode.GENERIC_STRUCTURAL_TYPE,
                GENERIC_TITLE_REVIEW_REASON,
                HierarchyIssueScope.CATALOG_TITLE,
                blocking=True,
                title=title,
            )

        missing_part_number = (
            title.part_type_manual == "part"
            and title.part_number_manual is None
        ) or (
            title.part_type_manual is None
            and title.effective_part_type == "part"
            and title.effective_part_number is None
        )
        if missing_part_number:
            add_issue(
                HierarchyIssueCode.MISSING_PART_NUMBER,
                MISSING_PART_NUMBER_REVIEW_REASON,
                HierarchyIssueScope.CATALOG_TITLE,
                blocking=True,
                title=title,
            )

        if (
            title.hierarchy_manual_override
            or title.hierarchy_verified_at is not None
        ) and (snapshot_issue := manual_hierarchy_snapshot_issue(title)):
            add_issue(
                HierarchyIssueCode.INCOMPLETE_MANUAL_SNAPSHOT,
                f"Historické ruční zařazení není úplné. {snapshot_issue}",
                HierarchyIssueScope.CATALOG_TITLE,
                blocking=False,
                title=title,
            )

    represented_confirmed_videos: set[Video] = set()
    for group in confirmed_duplicate_groups(all_videos):
        represented_confirmed_videos.update(group.videos)
        add_issue(
            HierarchyIssueCode.CONFIRMED_DUPLICATE,
            CONFIRMED_DUPLICATES_REVIEW_REASON,
            HierarchyIssueScope.VIDEO,
            blocking=True,
            title=_common_catalog_title(group.videos),
            target_videos=group.videos,
        )
    for video in all_videos:
        if (
            is_confirmed_duplicate(video)
            and video not in represented_confirmed_videos
        ):
            add_issue(
                HierarchyIssueCode.CONFIRMED_DUPLICATE,
                CONFIRMED_DUPLICATES_REVIEW_REASON,
                HierarchyIssueScope.VIDEO,
                blocking=True,
                title=_video_title(video, titles_by_id),
                target_videos=(video,),
            )

    if (
        include_legacy_fallback
        and collection.hierarchy_status in {"review_required", "conflict"}
        and not any(issue.blocking for issue in issues)
    ):
        note = (collection.hierarchy_note or "").strip()
        add_issue(
            HierarchyIssueCode.LEGACY_UNLOCALIZED_REVIEW_STATE,
            note or (
                "Collection obsahuje historický stav kontroly, jehož původní "
                "aktivní trigger již nelze bezpečně reprodukovat."
            ),
            HierarchyIssueScope.COLLECTION,
            blocking=True,
        )

    issue_tuple = tuple(issues)
    return HierarchyEvaluationResult(
        issues=issue_tuple,
        status=derive_hierarchy_status(collection, issue_tuple),
        primary_note=hierarchy_primary_note(issue_tuple),
    )


def apply_hierarchy_evaluation(
    collection: CatalogCollection,
    result: HierarchyEvaluationResult,
) -> None:
    """Persist only the status summary derived from a structured result."""
    collection.hierarchy_status = result.status
    collection.hierarchy_note = result.primary_note
    if result.status == "verified":
        collection.hierarchy_verified_at = (
            collection.hierarchy_verified_at or utc_now()
        )
    else:
        collection.hierarchy_verified_at = None


def finalize_collection_hierarchy(
    collection: CatalogCollection,
    videos: list[Video] | None = None,
    *,
    recalculate: bool = True,
    include_legacy_fallback: bool = False,
) -> HierarchyEvaluationResult:
    """Run the authoritative post-assignment structural/numbering evaluation."""
    all_videos = list(collection.videos if videos is None else videos)
    apply_automatic_structural_inference(collection)
    if recalculate:
        recalculate_collection_numbering(
            collection,
            {
                title.id: [
                    video for video in all_videos
                    if video.catalog_title is title
                    or video.catalog_title_id == title.id
                ]
                for title in collection.titles
            },
        )
    result = evaluate_collection_hierarchy(
        collection,
        all_videos,
        include_legacy_fallback=include_legacy_fallback,
    )
    apply_hierarchy_evaluation(collection, result)
    return result
