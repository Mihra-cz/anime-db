from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import re

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .catalog import detect_episode_number, derive_episode_number, normalize_title
from .models import CatalogCollection, CatalogTitle, Video, utc_now
from .numbering import collection_requires_numbering_review, recalculate_collection_numbering


PERIOD_HINT = re.compile(
    r"(?:\(|\s)([A-Z]\d{2}(?:-[A-Z]\d{2})?)(?:\)|\s*$)", re.IGNORECASE
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
class SingleSeasonSuggestion:
    title: CatalogTitle
    metadata_supports_tv: bool


def extract_local_period_hint(local_title: str) -> str | None:
    matches = PERIOD_HINT.findall(local_title or "")
    return matches[-1].upper() if matches else None


def collection_requires_review(collection: CatalogCollection, videos: list[Video]) -> str | None:
    if extract_local_period_hint(collection.local_title):
        return "Interní časový rozsah neurčuje bezpečně hranice sezón nebo částí."
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
    numbers = sorted({
        number for video in videos
        if (number := video.local_episode_number or derive_episode_number(video.filename)) is not None
    })
    if len(collection.titles) <= 1 and len(numbers) >= 14 and numbers == list(range(numbers[0], numbers[-1] + 1)):
        return "Souvislá řada epizod bez sezónních podsložek může obsahovat více částí."
    return None


def single_season_suggestion(
    collection: CatalogCollection,
) -> SingleSeasonSuggestion | None:
    if (
        len(collection.titles) != 1
        or collection.hierarchy_status in {"verified", "conflict", "not_applicable"}
        or collection.hierarchy_verified_at is not None
    ):
        return None
    title = collection.titles[0]
    if (
        not title.videos
        or title.effective_season_number is not None
        or title.effective_season_label is not None
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
    return SingleSeasonSuggestion(
        title=title,
        metadata_supports_tv=metadata_format in {"TV", "TV_SHORT"},
    )


def apply_single_season_suggestion(collection: CatalogCollection) -> CatalogTitle:
    suggestion = single_season_suggestion(collection)
    if suggestion is None:
        raise ValueError("Kolekce už nesplňuje podmínky bezpečného návrhu Season 1.")
    title = suggestion.title
    title.season_number_manual = 1
    title.season_label_manual = "S1"
    title.part_type_manual = "season"
    title.hierarchy_manual_override = True
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


def refresh_collection_state(collection: CatalogCollection) -> None:
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
    needs_review = (
        any(video.catalog_title is None and video.catalog_title_id is None for video in collection.videos)
        or collection_requires_numbering_review(collection)
    )
    collection.hierarchy_status = "review_required" if needs_review else "verified"
    collection.hierarchy_verified_at = None if needs_review else utc_now()
    collection.hierarchy_note = (
        "Číslování nebo nezařazený obsah stále vyžaduje kontrolu."
        if needs_review else None
    )


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
        if not video.content_type_manual and source_type in VIDEO_CONTENT_TYPES:
            video.content_type_manual = source_type
        video.catalog_title = target
        video.catalog_collection = collection
    target.hierarchy_manual_override = True
    target.hierarchy_verified_at = utc_now()
    session.flush()
    refresh_collection_state(collection)
    return target


def create_title_from_videos(
    session: Session, collection_id: int, video_ids: list[int], *,
    local_title: str, part_type: str,
) -> CatalogTitle:
    name = local_title.strip()[:200]
    normalized_type = part_type.strip().casefold()
    if not name:
        raise ValueError("Nová část musí mít název.")
    if normalized_type not in VIDEO_CONTENT_TYPES:
        raise ValueError("Neplatný typ doplňkového obsahu.")
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
    collection = _load_collection_for_assignment(session, collection_id)
    title = next((item for item in collection.titles if item.id == title_id), None)
    if title is None:
        raise ValueError("Část neexistuje v této kolekci.")
    if title.videos:
        raise ValueError("Odstranit lze pouze prázdnou část.")
    if not title.hierarchy_manual_override or "/.catalog-part-" not in title.relative_root_path:
        raise ValueError("Odstranit lze pouze lokálně vytvořenou virtuální část.")
    if title.metadata_record or title.external_links or title.metadata_candidates or title.artwork:
        raise ValueError("Část má metadata nebo další vazby a nelze ji bezpečně odstranit.")
    session.delete(title)


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
