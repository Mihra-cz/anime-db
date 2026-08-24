from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re

from .catalog import derive_episode_number
from .models import CatalogCollection, CatalogTitle, ManualSplitRuleVideo, Video
from .numbering import effective_video_numbering, is_nonprimary_duplicate_video


@dataclass(frozen=True)
class ManualTitleDefinition:
    title_id: int | None
    local_title: str
    manual_display_title: str | None
    season_number_manual: int | None
    season_label_manual: str | None
    part_number_manual: int | None
    part_type_manual: str | None
    episode_start: int | None
    episode_end: int | None
    episode_start_offset: int | None
    numbering_mode: str
    sort_order: int
    filename_pattern: str | None = None
    video_ids: tuple[int, ...] = ()


class ManualSplitDecisionKind(StrEnum):
    UNIQUE = "unique"
    CONFLICT = "conflict"
    UNMATCHED = "unmatched"
    NOT_REQUIRED = "not_required"


@dataclass(frozen=True)
class ManualSplitRule:
    """One rule and its persisted target, if the target already exists."""

    index: int
    definition: ManualTitleDefinition
    catalog_title: CatalogTitle | None = None


@dataclass(frozen=True)
class ManualSplitVideoDecision:
    """Complete immutable rule decision for one video before any assignment."""

    video: Video
    kind: ManualSplitDecisionKind
    matching_rules: tuple[ManualSplitRule, ...]

    @property
    def assigned_rule(self) -> ManualSplitRule | None:
        return self.matching_rules[0] if self.kind == ManualSplitDecisionKind.UNIQUE else None

    @property
    def target_catalog_title(self) -> CatalogTitle | None:
        rule = self.assigned_rule
        return rule.catalog_title if rule is not None else None

    @property
    def matching_catalog_titles(self) -> tuple[CatalogTitle, ...]:
        return tuple(
            rule.catalog_title
            for rule in self.matching_rules
            if rule.catalog_title is not None
        )


@dataclass(frozen=True)
class ManualSplitEvaluationResult:
    """Structured manual-split result shared by preview and every lifecycle path."""

    rules: tuple[ManualSplitRule, ...]
    decisions: tuple[ManualSplitVideoDecision, ...]

    @property
    def assignments(self) -> dict[int, int]:
        return {
            decision.video.id: decision.assigned_rule.index
            for decision in self.decisions
            if decision.video.id is not None and decision.assigned_rule is not None
        }

    @property
    def unmatched_video_ids(self) -> tuple[int, ...]:
        return tuple(
            decision.video.id
            for decision in self.decisions
            if decision.video.id is not None
            and decision.kind == ManualSplitDecisionKind.UNMATCHED
        )

    @property
    def conflicts(self) -> dict[int, tuple[int, ...]]:
        return {
            decision.video.id: tuple(rule.index for rule in decision.matching_rules)
            for decision in self.decisions
            if decision.video.id is not None
            and decision.kind == ManualSplitDecisionKind.CONFLICT
        }

    @property
    def unique_decisions(self) -> tuple[ManualSplitVideoDecision, ...]:
        return tuple(
            decision for decision in self.decisions
            if decision.kind == ManualSplitDecisionKind.UNIQUE
        )

    @property
    def conflict_decisions(self) -> tuple[ManualSplitVideoDecision, ...]:
        return tuple(
            decision for decision in self.decisions
            if decision.kind == ManualSplitDecisionKind.CONFLICT
        )

    @property
    def unmatched_decisions(self) -> tuple[ManualSplitVideoDecision, ...]:
        return tuple(
            decision for decision in self.decisions
            if decision.kind == ManualSplitDecisionKind.UNMATCHED
        )


# Compatibility name used by the existing preview/application API.
AssignmentPreview = ManualSplitEvaluationResult


def compile_manual_split_pattern(pattern: str) -> re.Pattern:
    if len(pattern) > 100 or "(?" in pattern or re.search(r"\\[1-9]", pattern):
        raise ValueError("Regulární pravidlo je příliš složité nebo dlouhé.")
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise ValueError("Regulární pravidlo není platné.") from exc


def _manual_split_number(
    video: Video, *, use_fresh_numbering: bool = False,
) -> int | None:
    if video.episode_number_manual_override is not None:
        return video.episode_number_manual_override
    if not use_fresh_numbering and video.local_episode_number is not None:
        return video.local_episode_number
    return derive_episode_number(video.filename)


def _same_collection(
    first: CatalogCollection | None,
    second: CatalogCollection | None,
) -> bool:
    if first is None or second is None:
        return first is second
    if first is second:
        return True
    return first.id is not None and first.id == second.id


def _requires_rule_assignment(
    video: Video,
    rules: tuple[ManualSplitRule, ...],
    *,
    persisted_targets: bool,
    use_fresh_numbering: bool,
) -> bool:
    """Preserve current authority boundaries for non-episodic/secondary content."""
    if (
        is_nonprimary_duplicate_video(video)
        or effective_video_numbering(
            video,
            use_current_title=not use_fresh_numbering,
        ).is_supplementary
    ):
        return False
    if persisted_targets and video.catalog_title is not None:
        target_titles = tuple(
            rule.catalog_title for rule in rules
            if rule.catalog_title is not None
        )
        target_collection = next(
            (title.collection for title in target_titles if title.collection is not None),
            None,
        )
        if (
            video.catalog_title not in target_titles
            and _same_collection(video.catalog_title.collection, target_collection)
        ):
            return False
    return True


def _rule_targets_current_assignment(
    rule: ManualSplitRule,
    video: Video,
) -> bool:
    target = rule.catalog_title
    if target is None:
        return False
    if video.catalog_title is target:
        return True
    return (
        target.id is not None
        and video.catalog_title_id is not None
        and target.id == video.catalog_title_id
    )


def evaluate_manual_split_assignment(
    videos: list[Video],
    definitions: list[ManualTitleDefinition],
    *,
    catalog_titles: list[CatalogTitle | None] | None = None,
    use_fresh_numbering: bool = False,
) -> ManualSplitEvaluationResult:
    """Evaluate every rule for every video before returning any assignment."""
    persisted_targets = catalog_titles is not None
    referenced_title_ids = [
        definition.title_id
        for definition in definitions
        if definition.title_id is not None
    ]
    if len(referenced_title_ids) != len(set(referenced_title_ids)):
        raise ValueError("Každá existující část smí být v pravidlech uvedena pouze jednou.")
    if not persisted_targets:
        available_video_ids = {
            video.id for video in videos if video.id is not None
        }
        requested_video_ids = {
            video_id
            for definition in definitions
            for video_id in definition.video_ids
        }
        if requested_video_ids - available_video_ids:
            raise ValueError(
                "Ruční rozdělení odkazuje na cizí nebo neexistující video."
            )
    targets = catalog_titles if catalog_titles is not None else [None] * len(definitions)
    if len(targets) != len(definitions):
        raise ValueError("Počet cílů ručního rozdělení neodpovídá počtu pravidel.")
    rules = tuple(
        ManualSplitRule(index, definition, targets[index])
        for index, definition in enumerate(definitions)
    )
    patterns = tuple(
        compile_manual_split_pattern(rule.definition.filename_pattern)
        if rule.definition.filename_pattern else None
        for rule in rules
    )
    has_any_selector = any(
        rule.definition.video_ids
        or rule.definition.episode_start is not None
        or pattern is not None
        for rule, pattern in zip(rules, patterns)
    )
    decisions: list[ManualSplitVideoDecision] = []
    for video in videos:
        number = _manual_split_number(
            video, use_fresh_numbering=use_fresh_numbering,
        )
        matching_rules = tuple(
            rule
            for rule, pattern in zip(rules, patterns)
            if (
                video.id in rule.definition.video_ids
                or (
                    rule.definition.episode_start is not None
                    and number is not None
                    and rule.definition.episode_start
                    <= number
                    <= rule.definition.episode_end
                )
                or bool(pattern and pattern.search(video.filename))
            )
        )
        current_target_rule = next(
            (
                rule for rule in rules
                if persisted_targets and _rule_targets_current_assignment(rule, video)
            ),
            None,
        )
        has_explicit_match = any(
            video.id in rule.definition.video_ids for rule in matching_rules
        )
        if (
            current_target_rule is not None
            and current_target_rule not in matching_rules
            and not has_explicit_match
            and len(matching_rules) <= 1
        ):
            # Staré DB neuchovávaly explicitní selection odděleně od assignmentu.
            # Nevysvětlitelný současný assignment proto konzervativně zachováme,
            # ale nevydáváme jej za rule match ani novou ruční autoritu.
            kind = ManualSplitDecisionKind.NOT_REQUIRED
            matching_rules = ()
        elif not has_any_selector:
            # Samotný obecný hierarchy override není manual-split membership
            # authority ani důvod vyžadovat pokrytí videí pravidly.
            kind = ManualSplitDecisionKind.NOT_REQUIRED
        elif len(matching_rules) == 1:
            kind = ManualSplitDecisionKind.UNIQUE
        elif len(matching_rules) > 1:
            kind = ManualSplitDecisionKind.CONFLICT
        elif rules and _requires_rule_assignment(
            video,
            rules,
            persisted_targets=persisted_targets,
            use_fresh_numbering=use_fresh_numbering,
        ):
            kind = ManualSplitDecisionKind.UNMATCHED
        else:
            kind = ManualSplitDecisionKind.NOT_REQUIRED
        decisions.append(ManualSplitVideoDecision(video, kind, matching_rules))
    return ManualSplitEvaluationResult(rules, tuple(decisions))


def definition_from_title(title: CatalogTitle) -> ManualTitleDefinition:
    return ManualTitleDefinition(
        title_id=title.id,
        local_title=title.local_title,
        manual_display_title=title.manual_display_title,
        season_number_manual=title.season_number_manual,
        season_label_manual=title.season_label_manual,
        part_number_manual=title.part_number_manual,
        part_type_manual=title.part_type_manual,
        episode_start=title.episode_start,
        episode_end=title.episode_end,
        episode_start_offset=title.episode_start_offset,
        numbering_mode=title.numbering_mode,
        sort_order=title.effective_sort_order,
        filename_pattern=title.episode_filename_pattern,
        video_ids=tuple(sorted(
            link.video_id for link in title.manual_split_rule_videos
        )),
    )


def synchronize_manual_split_authority(
    definitions: list[ManualTitleDefinition],
    catalog_titles: list[CatalogTitle],
    videos: list[Video],
) -> None:
    """Replace explicit rule memberships without changing resulting assignments."""
    if len(catalog_titles) != len(definitions):
        raise ValueError("Počet cílů ručního rozdělení neodpovídá počtu pravidel.")
    videos_by_id = {
        video.id: video for video in videos if video.id is not None
    }
    requested_ids = {
        video_id
        for definition in definitions
        for video_id in definition.video_ids
    }
    unknown_ids = requested_ids - videos_by_id.keys()
    if unknown_ids:
        raise ValueError(
            "Ruční rozdělení odkazuje na cizí nebo neexistující video."
        )

    for definition, title in zip(definitions, catalog_titles):
        desired_ids = set(definition.video_ids)
        existing = {
            link.video_id: link for link in title.manual_split_rule_videos
        }
        for video_id in existing.keys() - desired_ids:
            link = existing[video_id]
            title.manual_split_rule_videos.remove(link)
            if link in link.video.manual_split_rule_videos:
                link.video.manual_split_rule_videos.remove(link)
        for video_id in desired_ids - existing.keys():
            title.manual_split_rule_videos.append(ManualSplitRuleVideo(
                video=videos_by_id[video_id],
            ))


def has_persisted_manual_split_selector(title: CatalogTitle) -> bool:
    return bool(
        title.manual_split_rule_videos
        or title.episode_start is not None
        or title.episode_end is not None
        or title.episode_filename_pattern
    )


def manual_split_titles(collection: CatalogCollection) -> list[CatalogTitle]:
    return [
        title for title in collection.titles
        if has_persisted_manual_split_selector(title)
    ]


def replace_explicit_video_selector_authority(
    videos: list[Video],
    target: CatalogTitle | None,
) -> None:
    """Persist the exact selector set for an explicit user video decision."""
    if target is not None and target.id is None:
        raise ValueError("Cílová část musí být před uložením selectoru persistentní.")
    target_id = target.id if target is not None else None
    for video in videos:
        matching_target = False
        for link in list(video.manual_split_rule_videos):
            if target is not None and (
                link.catalog_title is target
                or link.catalog_title_id == target_id
            ):
                matching_target = True
                continue
            video.manual_split_rule_videos.remove(link)
            if link in link.catalog_title.manual_split_rule_videos:
                link.catalog_title.manual_split_rule_videos.remove(link)
        if target is not None and not matching_target:
            target.manual_split_rule_videos.append(ManualSplitRuleVideo(video=video))


def persisted_manual_split_authority_collections(
    video: Video,
) -> tuple[CatalogCollection, ...]:
    """Return deterministic collection targets proven by explicit M:N authority."""
    collections: dict[tuple[str, int | str], CatalogCollection] = {}
    for link in video.manual_split_rule_videos:
        title = link.catalog_title
        collection = title.collection if title is not None else None
        if collection is None:
            continue
        key = (
            "id", collection.id,
        ) if collection.id is not None else (
            "path", collection.relative_root_path,
        )
        collections[key] = collection
    return tuple(sorted(
        collections.values(),
        key=lambda collection: (collection.relative_root_path, collection.id or 0),
    ))


def evaluate_persisted_manual_split(
    collection: CatalogCollection,
    videos: list[Video] | None = None,
    *,
    use_fresh_numbering: bool = False,
) -> ManualSplitEvaluationResult:
    titles = sorted(
        manual_split_titles(collection),
        key=lambda title: (
            title.effective_sort_order,
            title.id if title.id is not None else 0,
        ),
    )
    return evaluate_manual_split_assignment(
        list(collection.videos if videos is None else videos),
        [definition_from_title(title) for title in titles],
        catalog_titles=list(titles),
        use_fresh_numbering=use_fresh_numbering,
    )


def historical_manual_split_ambiguities(
    collection: CatalogCollection,
    result: ManualSplitEvaluationResult,
) -> tuple[ManualSplitVideoDecision, ...]:
    """Locate unresolvable pre-authority conflicts without guessing candidates."""
    if collection.hierarchy_status != "conflict":
        return ()
    explicit_video_ids = {
        video_id
        for rule in result.rules
        for video_id in rule.definition.video_ids
    }
    return tuple(
        decision
        for decision in result.decisions
        if decision.video.id is not None
        and decision.video.catalog_title_id is None
        and decision.video.id not in explicit_video_ids
        and decision.kind != ManualSplitDecisionKind.CONFLICT
    )


def apply_manual_split_decisions(
    result: ManualSplitEvaluationResult,
    collection: CatalogCollection,
    *,
    catalog_titles: list[CatalogTitle] | None = None,
) -> None:
    """Apply an already complete decision; this function never matches rules."""
    targets = catalog_titles or [
        rule.catalog_title for rule in result.rules
        if rule.catalog_title is not None
    ]
    if len(targets) != len(result.rules):
        raise ValueError("Nelze aplikovat ruční rozdělení bez všech cílových částí.")
    for decision in result.decisions:
        decision.video.catalog_collection = collection
        if decision.kind == ManualSplitDecisionKind.UNIQUE:
            target = targets[decision.assigned_rule.index]
            decision.video.catalog_title = target
            decision.video.catalog_title_id = target.id
        elif decision.kind in {
            ManualSplitDecisionKind.CONFLICT,
            ManualSplitDecisionKind.UNMATCHED,
        }:
            decision.video.catalog_title = None
            decision.video.catalog_title_id = None
