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

from .catalog import GENERIC_ROOTS, detect_episode_number, derive_episode_number, normalize_title
from .hierarchy import parse_explicit_part
from .models import (
    CatalogCollection, CatalogTitle, CollectionGroupingDecision, Video, utc_now,
)
from .numbering import (
    clear_duplicate_group, collection_requires_numbering_review,
    is_confirmed_duplicate, recalculate_collection_numbering,
    set_duplicate_group_primary, supplementary_context_map,
    video_numbering_identity,
)


PERIOD_HINT = re.compile(
    r"(?:\(|\s)([A-Z]\d{2}(?:-[A-Z]\d{2})?)(?:\)|\s*$)", re.IGNORECASE
)
PERIOD_HINT_REVIEW_REASON = (
    "Interní časový rozsah neurčuje bezpečně hranice sezón nebo částí."
)
CONFIRMED_DUPLICATES_REVIEW_REASON = (
    "Potvrzené duplicitní soubory vyžadují vyřešení."
)
MISSING_DUPLICATE_PRIMARY_REVIEW_REASON = (
    "Primární video potvrzené duplicity chybí; vztah vyžaduje novou ruční kontrolu."
)
PROBABLE_GROUPING_REVIEW_REASON = (
    "Část s vlastním názvem byla seskupena podle společného fyzického parentu a "
    "příbuzného názvu; vztah vyžaduje ruční potvrzení."
)
SUPPLEMENTARY_CONTEXT_REVIEW_REASON = (
    "Doplňková část zachovává kontext vlastního child názvu, ale související "
    "season vyžaduje ruční potvrzení."
)
ALLOWED_PART_TYPES = {
    "title", "season", "part", "cour", "film", "ova", "special",
    "preview", "recap", "bonus", "other",
}
SUPPLEMENTAL_PART_TYPES = {"film", "ova", "special", "preview", "recap", "bonus", "other"}
VIDEO_CONTENT_TYPES = {"preview", "special", "recap", "ova", "bonus", "other"}
ALLOWED_NUMBERING_MODES = {"unknown", "season_local", "absolute", "mixed"}
SIMPLE_DEFINITION_FIELDS = (
    "title_id", "local_title", "manual_display_title", "season_number_manual",
    "season_label_manual", "part_number", "part_type_manual", "episode_start",
    "episode_end", "episode_start_offset", "numbering_mode", "sort_order",
    "filename_pattern", "video_ids",
)


@dataclass(frozen=True)
class ManualTitleDefinition:
    title_id: int | None
    local_title: str
    manual_display_title: str | None
    season_number_manual: int | None
    season_label_manual: str | None
    part_number: int | None
    part_type_manual: str | None
    episode_start: int | None
    episode_end: int | None
    episode_start_offset: int | None
    numbering_mode: str
    sort_order: int
    filename_pattern: str | None = None
    video_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class AssignmentPreview:
    assignments: dict[int, int]
    unmatched_video_ids: tuple[int, ...]
    conflicts: dict[int, tuple[int, ...]]


@dataclass(frozen=True)
class SingleTitleConfirmationSuggestion:
    title: CatalogTitle
    metadata_supports_tv: bool
    proposed_part_type: str
    proposed_season_number: int | None
    proposed_season_label: str | None


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
        "bonus": "Bonus",
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
        manual_display_title=name, hierarchy_status="verified",
        hierarchy_verified_at=utc_now(),
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
    now = utc_now()
    for title in titles:
        title.collection = target
        title.hierarchy_manual_override = True
        title.hierarchy_verified_at = now
        for video in title.videos:
            video.catalog_collection = target
    session.flush()
    for collection in sources | {target}:
        session.expire(collection, ["titles", "videos"])
        if not collection.titles and not collection.videos:
            collection.hierarchy_status = "verified"
            collection.hierarchy_verified_at = now
            collection.hierarchy_note = None
        else:
            refresh_collection_state(collection)
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


def manual_hierarchy_resolves_ambiguity(collection: CatalogCollection) -> bool:
    """Rozliší potvrzenou strukturu od pouhého obecného statusu verified."""
    return bool(collection.titles) and all(
        title.hierarchy_manual_override
        and (
            title.season_number_manual is not None
            or title.season_label_manual is not None
            or title.part_type_manual is not None
            or title.episode_start is not None
            or title.episode_end is not None
            or bool(title.episode_filename_pattern)
        )
        for title in collection.titles
    )


def collection_requires_review(collection: CatalogCollection, videos: list[Video]) -> str | None:
    hierarchy_resolved = manual_hierarchy_resolves_ambiguity(collection)
    if extract_local_period_hint(collection.local_title) and not hierarchy_resolved:
        return PERIOD_HINT_REVIEW_REASON
    nonstandard = [
        detection.display_value
        for video in videos
        if (detection := detect_episode_number(video.filename)).is_nonstandard
        and not video.content_type_manual
        and (
            video.catalog_title is None
            or video.catalog_title.effective_part_type not in SUPPLEMENTAL_PART_TYPES
        )
    ]
    if nonstandard:
        return "Nestandardní číslování vyžaduje ruční zařazení."
    if any(video.duplicate_primary_missing for video in videos):
        return MISSING_DUPLICATE_PRIMARY_REVIEW_REASON
    if any(is_confirmed_duplicate(video) for video in videos):
        return CONFIRMED_DUPLICATES_REVIEW_REASON
    numbers = sorted({
        number for video in videos
        if not video.content_type_manual
        and not detect_episode_number(video.filename).is_supplementary
        and (number := video.local_episode_number or derive_episode_number(video.filename)) is not None
    })
    if (
        not hierarchy_resolved
        and len(collection.titles) <= 1
        and len(numbers) >= 14
        and numbers == list(range(numbers[0], numbers[-1] + 1))
    ):
        return "Souvislá řada epizod bez sezónních podsložek může obsahovat více částí."
    return None


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
) -> CatalogTitle:
    suggestion = single_title_confirmation_suggestion(collection)
    if suggestion is None:
        raise ValueError("Kolekce už nesplňuje podmínky ručního potvrzení jediné části.")
    normalized_type = part_type.strip().casefold()
    if normalized_type not in ALLOWED_PART_TYPES:
        raise ValueError("Neplatný typ části.")
    if season_number is not None and season_number <= 0:
        raise ValueError("Číslo sezóny musí být kladné.")
    label = (season_label or "").strip()[:50] or None
    if normalized_type == "season":
        if season_number is not None and label is None:
            label = f"S{season_number}"
    else:
        season_number = None
        label = None
    title = suggestion.title
    title.season_number_manual = season_number
    title.season_label_manual = label
    title.part_type_manual = normalized_type
    title.hierarchy_manual_override = True
    title.hierarchy_verified_at = utc_now()
    refresh_collection_state(collection, recalculate=False)
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
            "title_id", "season_number_manual", "part_number", "episode_start",
            "episode_end", "episode_start_offset", "sort_order",
        ):
            raw_value = value.get(field)
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
        if part_type and part_type not in ALLOWED_PART_TYPES:
            raise ValueError(f"Část {position} má neplatný typ.")
        mode = str(value.get("numbering_mode") or "unknown").strip().casefold()
        if mode not in ALLOWED_NUMBERING_MODES:
            raise ValueError(f"Část {position} má neplatný režim číslování.")
        pattern = str(value.get("filename_pattern") or "").strip() or None
        if pattern:
            _compile_safe_pattern(pattern)
        try:
            video_ids = tuple(int(item) for item in (value.get("video_ids") or []))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Část {position}: video_ids musí být seznam ID.") from exc
        definitions.append(ManualTitleDefinition(
            title_id=integer_fields["title_id"], local_title=local_title,
            manual_display_title=_optional_text(value.get("manual_display_title"), 200),
            season_number_manual=integer_fields["season_number_manual"],
            season_label_manual=_optional_text(value.get("season_label_manual"), 50),
            part_number=integer_fields["part_number"], part_type_manual=part_type,
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
        rows.append({
            "title_id": title.id,
            "local_title": title.local_title,
            "manual_display_title": title.manual_display_title,
            "season_number_manual": title.season_number_manual,
            "season_label_manual": title.season_label_manual,
            "part_number": title.part_number,
            "part_type_manual": title.part_type_manual,
            "episode_start": title.episode_start,
            "episode_end": title.episode_end,
            "episode_start_offset": title.episode_start_offset,
            "numbering_mode": title.numbering_mode,
            "sort_order": title.effective_sort_order,
            "filename_pattern": title.episode_filename_pattern,
            "video_ids": "",
        })
    rows.append({field: "" for field in SIMPLE_DEFINITION_FIELDS})
    return rows


def _optional_text(value, limit: int) -> str | None:
    return str(value or "").strip()[:limit] or None


def _compile_safe_pattern(pattern: str) -> re.Pattern:
    if len(pattern) > 100 or "(?" in pattern or re.search(r"\\[1-9]", pattern):
        raise ValueError("Regulární pravidlo je příliš složité nebo dlouhé.")
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise ValueError("Regulární pravidlo není platné.") from exc


def preview_assignments(
    videos: list[Video], definitions: list[ManualTitleDefinition]
) -> AssignmentPreview:
    assignments, conflicts, unmatched = {}, {}, []
    patterns = [
        _compile_safe_pattern(definition.filename_pattern)
        if definition.filename_pattern else None for definition in definitions
    ]
    for video in videos:
        number = video.episode_number_manual_override
        if number is None:
            number = video.local_episode_number
        if number is None:
            number = derive_episode_number(video.filename)
        matches = []
        for index, (definition, pattern) in enumerate(zip(definitions, patterns)):
            range_match = (
                definition.episode_start is not None and number is not None
                and definition.episode_start <= number <= definition.episode_end
            )
            if video.id in definition.video_ids or range_match or pattern and pattern.search(video.filename):
                matches.append(index)
        if len(matches) == 1:
            assignments[video.id] = matches[0]
        elif len(matches) > 1:
            conflicts[video.id] = tuple(matches)
        else:
            unmatched.append(video.id)
    return AssignmentPreview(assignments, tuple(unmatched), conflicts)


def definition_from_title(title: CatalogTitle) -> ManualTitleDefinition:
    return ManualTitleDefinition(
        title_id=title.id, local_title=title.local_title,
        manual_display_title=title.manual_display_title,
        season_number_manual=title.season_number_manual,
        season_label_manual=title.season_label_manual, part_number=title.part_number,
        part_type_manual=title.part_type_manual, episode_start=title.episode_start,
        episode_end=title.episode_end, episode_start_offset=title.episode_start_offset,
        numbering_mode=title.numbering_mode, sort_order=title.effective_sort_order,
        filename_pattern=title.episode_filename_pattern,
        video_ids=tuple(video.id for video in title.videos),
    )


def manual_split_titles(collection: CatalogCollection) -> list[CatalogTitle]:
    return [title for title in collection.titles if title.hierarchy_manual_override]


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
    if recalculate:
        recalculate_collection_numbering(
            collection,
            {
                title.id: [
                    video for video in collection.videos
                    if video.catalog_title is title or video.catalog_title_id == title.id
                ]
                for title in collection.titles
            },
        )
    has_unassigned = any(
        video.catalog_title is None and video.catalog_title_id is None
        for video in collection.videos
    )
    has_numbering_problem = collection_requires_numbering_review(collection)
    hierarchy_reason = collection_requires_review(collection, list(collection.videos))
    if has_unassigned:
        note = "Nové nebo nezařazené video vyžaduje kontrolu."
    elif has_numbering_problem:
        note = "Číslování nebo nezařazený obsah stále vyžaduje kontrolu."
    else:
        note = hierarchy_reason
    collection.hierarchy_status = "review_required" if note else "verified"
    collection.hierarchy_verified_at = None if note else utc_now()
    collection.hierarchy_note = note


def classify_videos_in_place(
    session: Session, collection_id: int, video_ids: list[int], content_type: str,
) -> list[Video]:
    normalized_type = content_type.strip().casefold()
    if normalized_type not in VIDEO_CONTENT_TYPES:
        raise ValueError("Neplatný typ doplňkového obsahu.")
    collection = _load_collection_for_assignment(session, collection_id)
    selected = _selected_videos(collection, video_ids)
    for video in selected:
        video.content_type_manual = normalized_type
        if video.catalog_title is not None:
            video.catalog_title.hierarchy_manual_override = True
            video.catalog_title.hierarchy_verified_at = utc_now()
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
        source_type = (
            video.catalog_title.effective_part_type if video.catalog_title is not None else None
        )
        detected_type = detect_episode_number(video.filename).supplementary_type
        target_type = target.effective_part_type
        if not video.content_type_manual:
            video.content_type_manual = (
                target_type if target_type in VIDEO_CONTENT_TYPES else
                source_type if source_type in VIDEO_CONTENT_TYPES else
                detected_type if detected_type in VIDEO_CONTENT_TYPES else
                "bonus" if detected_type else None
            )
        video.catalog_title = target
        video.catalog_collection = collection
    target.hierarchy_manual_override = True
    target.hierarchy_verified_at = utc_now()
    session.flush()
    refresh_collection_state(collection)
    return target


def create_title_from_videos(
    session: Session, collection_id: int, video_ids: list[int], *,
    local_title: str, part_type: str, season_number: int | None = None,
    season_label: str | None = None,
) -> CatalogTitle:
    name = local_title.strip()[:200]
    normalized_type = part_type.strip().casefold()
    if not name:
        raise ValueError("Nová část musí mít název.")
    if normalized_type not in VIDEO_CONTENT_TYPES:
        raise ValueError("Neplatný typ doplňkového obsahu.")
    if season_number is not None and season_number <= 0:
        raise ValueError("Číslo související sezóny musí být kladné.")
    normalized_label = (season_label or "").strip()[:50] or (
        f"S{season_number}" if season_number is not None else None
    )
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
        relative_root_path=virtual_path, part_type_manual=normalized_type,
        season_number_manual=season_number, season_label_manual=normalized_label,
        sort_order_manual=position, hierarchy_manual_override=True,
        hierarchy_verified_at=utc_now(), numbering_mode="unknown",
    )
    session.add(title)
    session.flush()
    for video in selected:
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
    session: Session, collection_id: int, title_id: int,
) -> None:
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
    video_count = session.scalar(select(func.count(Video.id)).where(
        Video.catalog_title_id == title_id
    )) or 0
    if video_count:
        raise ValueError(
            "Část už není prázdná; obsahuje video a nebyla odstraněna."
        )
    # Všechny čtyři vztahy jsou vlastněné CatalogTitle přes delete-orphan a jejich
    # FK mají ON DELETE CASCADE. Explicitní vyprázdnění udrží chování stejné i
    # tam, kde SQLite foreign_keys není zapnuté.
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


def separate_nonstandard_videos(
    session: Session, collection_id: int, video_ids: list[int], *,
    local_title: str, part_type: str,
) -> CatalogTitle:
    """Logicky oddělí vybraná nestandardní videa bez změny jejich cesty."""
    collection = _load_collection_for_assignment(session, collection_id)
    selected = _selected_videos(collection, video_ids)
    if any(not detect_episode_number(video.filename).is_nonstandard for video in selected):
        raise ValueError("Oddělit lze touto akcí pouze rozpoznaný nestandardní obsah.")
    return create_title_from_videos(
        session, collection_id, video_ids, local_title=local_title, part_type=part_type
    )


def apply_manual_split(
    session: Session, collection_id: int, definitions: list[ManualTitleDefinition],
    *, confirm_conflicts: bool = False,
) -> AssignmentPreview:
    collection = session.scalar(select(CatalogCollection).options(
        selectinload(CatalogCollection.titles), selectinload(CatalogCollection.videos),
    ).where(CatalogCollection.id == collection_id))
    if collection is None:
        raise ValueError("Kolekce nebyla nalezena.")
    preview = preview_assignments(collection.videos, definitions)
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
        title.season_number_manual = definition.season_number_manual
        title.season_label_manual = definition.season_label_manual
        title.part_number = definition.part_number
        title.part_type_manual = definition.part_type_manual
        title.episode_start = definition.episode_start
        title.episode_end = definition.episode_end
        title.episode_start_offset = definition.episode_start_offset
        title.numbering_mode = definition.numbering_mode
        title.sort_order_manual = definition.sort_order
        title.episode_filename_pattern = definition.filename_pattern
        title.hierarchy_manual_override = True
        title.hierarchy_verified_at = now
        resolved.append(title)
    session.flush()
    for video in collection.videos:
        target_index = preview.assignments.get(video.id)
        video.catalog_title = resolved[target_index] if target_index is not None else None
        video.catalog_collection = collection
    collection.hierarchy_status = "conflict" if preview.conflicts else (
        "review_required" if preview.unmatched_video_ids else "verified"
    )
    collection.hierarchy_verified_at = now if collection.hierarchy_status == "verified" else None
    collection.hierarchy_note = (
        "Konflikt překrývajících se pravidel."
        if preview.conflicts else "Nové nebo nezařazené video."
        if preview.unmatched_video_ids else None
    )
    recalculate_collection_numbering(
        collection,
        {title.id: [video for video in collection.videos if video.catalog_title is title]
         for title in resolved},
    )
    return preview


def definitions_as_json(collection: CatalogCollection) -> str:
    values = []
    for title in sorted(collection.titles, key=lambda item: item.effective_sort_order):
        values.append({
            "title_id": title.id, "local_title": title.local_title,
            "manual_display_title": title.manual_display_title,
            "season_number_manual": title.season_number_manual,
            "season_label_manual": title.season_label_manual,
            "part_number": title.part_number, "part_type_manual": title.part_type_manual,
            "episode_start": title.episode_start, "episode_end": title.episode_end,
            "episode_start_offset": title.episode_start_offset,
            "numbering_mode": title.numbering_mode, "sort_order": title.effective_sort_order,
            "filename_pattern": title.episode_filename_pattern,
            "video_ids": [],
        })
    return json.dumps(values, ensure_ascii=False, indent=2)


def definitions_to_json(definitions: list[ManualTitleDefinition]) -> str:
    return json.dumps(
        [
            {**definition.__dict__, "video_ids": list(definition.video_ids)}
            for definition in definitions
        ],
        ensure_ascii=False,
        indent=2,
    )
