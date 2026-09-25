"""Explicit, transactional save scope for ordinary hierarchy detail edits."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy.orm import Session

from .catalog import (
    catalog_video_identity, effective_video_content_display,
    effective_video_content_type,
)
from .hierarchy_evaluation import (
    catalog_title_hierarchy_is_verified, strict_hierarchy_write_guard,
)
from .hierarchy_review import (
    classify_videos_in_place, refresh_collection_state,
    set_manual_title_hierarchy,
)
from .hierarchy_types import (
    LEGACY_PART_TYPES, PART_TYPE_LABELS, VIDEO_CONTENT_TYPE_CHOICES,
)
from .media_parts import MEDIA_PART_NUMBER_ERROR, set_media_part_number
from .models import CatalogTitle, Video, VideoVariantGroup
from .numbering import (
    PART_LOCAL_NUMBERING_MODE,
    manual_episode_number_input_value, recalculate_title_numbering,
    set_title_numbering, set_video_episode_number_from_input,
)
from .video_variants import update_video_variant_group_for_title


class MissingEditTarget(ValueError):
    """The submitted edit no longer belongs to the expected page."""


@dataclass(frozen=True)
class EditChangeGroup:
    heading: str
    changes: tuple[str, ...]


@dataclass(frozen=True)
class HierarchyPageEditResult:
    saved_sections: int
    change_groups: tuple[EditChangeGroup, ...]


TITLE_HIERARCHY_CONDITIONAL_FIELDS = (
    "season_number_manual", "season_label_manual", "part_number_manual",
)
_TITLE_HIERARCHY_REQUIRED_FIELDS = frozenset({
    "part_type_manual", "sort_order_manual", "numbering_mode",
    "episode_start_offset",
})


def title_hierarchy_conditional_fields(part_type_manual: str) -> frozenset[str]:
    """Structural inputs the title hierarchy form uses for one part type.

    ``hierarchy_fields.js`` hides and disables the other conditional inputs, so
    the browser omits them from both the local submit and Save All.  An omitted
    input therefore means "not used by this part type", never a damaged request.
    """
    part_type = part_type_manual.strip().casefold()
    if not part_type:
        return frozenset()
    fields = {"season_number_manual"}
    if part_type != "part":
        fields.add("season_label_manual")
    if part_type in {"season", "part", "cour"}:
        fields.add("part_number_manual")
    return frozenset(fields)


def missing_title_hierarchy_fields(
    part_type_manual: object, submitted: Iterable[str],
) -> frozenset[str]:
    """Return the relevant title form inputs a submission lacks."""
    part_type = part_type_manual if isinstance(part_type_manual, str) else ""
    return (
        _TITLE_HIERARCHY_REQUIRED_FIELDS
        | title_hierarchy_conditional_fields(part_type)
    ) - set(submitted)


def apply_title_numbering_edit(
    session: Session, title: CatalogTitle, numbering_mode: str,
    episode_start_offset: str,
) -> None:
    try:
        offset = int(episode_start_offset) if episode_start_offset.strip() else None
    except ValueError as exc:
        raise ValueError("Offset musí být nezáporné celé číslo.") from exc
    with strict_hierarchy_write_guard(
        session, [title.collection] if title.collection is not None else [],
    ):
        set_title_numbering(
            title, "unknown" if numbering_mode == "auto" else numbering_mode,
            offset,
        )
        if title.collection is not None:
            refresh_collection_state(title.collection)
        else:
            recalculate_title_numbering(title, list(title.videos))


def apply_title_hierarchy_edit(
    session: Session, collection_id: int, title_id: int, *,
    season_number_manual: str, season_label_manual: str,
    part_number_manual: str, part_type_manual: str,
    sort_order_manual: str, hierarchy_verified: bool,
) -> None:
    title = session.get(CatalogTitle, title_id)
    if title is None or title.catalog_collection_id != collection_id:
        raise MissingEditTarget("Část kolekce nebyla nalezena")
    try:
        number = int(season_number_manual) if season_number_manual.strip() else None
        part_number = int(part_number_manual) if part_number_manual.strip() else None
        order = int(sort_order_manual) if sort_order_manual.strip() else None
    except ValueError as exc:
        raise ValueError(
            "Číslo sezóny, Part a ruční pořadí musí být celá čísla."
        ) from exc
    set_manual_title_hierarchy(
        title, season_number=number, season_label=season_label_manual,
        part_type=part_type_manual, sort_order=order,
        hierarchy_verified=hierarchy_verified, part_number=part_number,
    )


def _human_value(value: object) -> str:
    if value is True:
        return "ano"
    if value is False:
        return "ne"
    return str(value) if value not in {None, ""} else "neurčeno"


def apply_title_hierarchy_form_edit(
    session: Session, collection_id: int, title_id: int, *,
    part_type_manual: str, season_number_manual: str | None,
    season_label_manual: str | None, part_number_manual: str | None,
    sort_order_manual: str, hierarchy_verified: bool,
    numbering_mode: str, episode_start_offset: str,
) -> EditChangeGroup:
    """Stage the ordinary title hierarchy form and describe its impact.

    ``None`` is a conditional input the form omitted because the submitted part
    type does not use it.  It is not an edit by itself, so a numbering-only save
    never rewrites the structure; when the structure is rewritten, it is empty.
    """
    title = session.get(CatalogTitle, title_id)
    if title is None or title.catalog_collection_id != collection_id:
        raise MissingEditTarget("Část už nepatří do této kolekce.")
    submitted = (
        part_type_manual.strip(),
        *(
            None if value is None else value.strip()
            for value in (
                season_number_manual, season_label_manual, part_number_manual,
            )
        ),
        sort_order_manual.strip(), hierarchy_verified, numbering_mode.strip(),
        episode_start_offset.strip(),
    )
    requested = tuple("" if value is None else value for value in submitted)
    current = (
        title.part_type_manual or "",
        str(title.season_number_manual or ""),
        title.season_label_manual or "",
        str(title.part_number_manual or ""),
        str(title.sort_order_manual if title.sort_order_manual is not None else ""),
        catalog_title_hierarchy_is_verified(title),
        "auto" if title.numbering_mode == "unknown" else title.numbering_mode,
        str(title.episode_start_offset if title.episode_start_offset is not None else ""),
    )
    labels = (
        "Typ části", "Číslo sezóny", "Označení sezóny", "Číslo Part",
        "Ruční pořadí", "Zařazení ověřeno", "Režim číslování",
        (
            "Offset zdrojového číslování"
            if requested[6] == PART_LOCAL_NUMBERING_MODE
            else "Počet předchozích epizod"
        ),
    )
    structure_changed = any(
        value is not None and value != before
        for value, before in zip(submitted[:6], current[:6], strict=True)
    )
    # The form renders a legacy type only so an already stored snapshot survives
    # an unrelated edit; it can be kept unchanged or converted, never newly
    # chosen, edited or promoted from a historical snapshot.
    requested_type = requested[0].casefold()
    if requested_type in LEGACY_PART_TYPES and (
        requested_type != current[0] or structure_changed
    ):
        raise ValueError(
            f"Typ části {PART_TYPE_LABELS[requested_type]} je pouze legacy hodnota; "
            "lze jej zachovat beze změny nebo převést na Sezónu či Part."
        )
    changes = tuple(
        f"{label}: {_human_value(before)} → {_human_value(after)}"
        for index, (label, before, after) in enumerate(
            zip(labels, current, requested, strict=True)
        )
        if before != after and (structure_changed or index >= 6)
    )
    if not changes:
        raise ValueError(
            "Formulář neobsahuje žádnou změněnou hierarchy hodnotu."
        )
    if structure_changed:
        apply_title_hierarchy_edit(
            session, collection_id, title_id,
            season_number_manual=requested[1], season_label_manual=requested[2],
            part_number_manual=requested[3], part_type_manual=requested[0],
            sort_order_manual=requested[4], hierarchy_verified=requested[5],
        )
    if requested[6:] != current[6:]:
        apply_title_numbering_edit(
            session, title, requested[6], requested[7],
        )
    return EditChangeGroup(f"Struktura části: {title.local_title}", changes)


def apply_episode_position_edit(
    session: Session, video_id: int, raw_value: str, *,
    expected_collection_id: int | None = None,
    recap_only: bool = False,
) -> int:
    video = session.get(Video, video_id)
    if video is None or video.catalog_title_id is None:
        raise MissingEditTarget("Video nebylo nalezeno")
    title = video.catalog_title
    if expected_collection_id is not None and (
        title.collection is None or title.collection.id != expected_collection_id
    ):
        raise ValueError("Video nepatří do této kolekce.")
    if recap_only and effective_video_content_type(video) != "recap":
        raise ValueError("Hromadně lze uložit pouze ruční pozici Recapu.")
    with strict_hierarchy_write_guard(
        session, [title.collection] if title.collection is not None else [],
    ):
        set_video_episode_number_from_input(video, raw_value)
        if title.collection is not None:
            refresh_collection_state(title.collection)
        else:
            recalculate_title_numbering(title, list(title.videos))
    return title.id


_ORDINAL_FIELD_LABELS = {
    "film": "Ordinal Filmu", "special": "Ordinal Specialu",
    "ova": "Ordinal OVA", "preview": "Ordinal Preview",
    "bonus": "Ordinal Bonusu", "other": "Ordinal jiného obsahu",
    "op": "Ordinal OP", "ed": "Ordinal ED", "ncop": "Ordinal NCOP",
    "nced": "Ordinal NCED", "cm": "Ordinal CM", "menu": "Ordinal Menu",
}


def hierarchy_number_field_label(content_type: str) -> str:
    if content_type == "episode":
        return "Ruční číslo epizody"
    if content_type == "recap":
        return "Ruční pozice Recapu"
    return _ORDINAL_FIELD_LABELS.get(content_type, "Ruční ordinal")


def apply_title_video_hierarchy_edit(
    session: Session, collection_id: int, title_id: int, video_id: int, *,
    manual_episode_number: str, media_part_number: str, content_type: str,
) -> list[str]:
    """Stage compatible hierarchy axes of one video and describe the semantics."""
    video = session.get(Video, video_id)
    if (
        video is None
        or video.catalog_title_id != title_id
        or video.catalog_collection_id != collection_id
    ):
        raise MissingEditTarget("Video už nepatří do této části a kolekce.")
    title = video.catalog_title
    current_identity = catalog_video_identity(video, title)
    before_content = effective_video_content_display(video)
    before_episode = str(manual_episode_number_input_value(video) or "")
    changes: list[str] = []
    normalized_content = content_type.strip().casefold()
    if normalized_content != (video.content_type_manual or ""):
        classify_videos_in_place(
            session, collection_id, [video.id], normalized_content,
        )
        labels = dict(VIDEO_CONTENT_TYPE_CHOICES)
        after_content_label = effective_video_content_display(video).label
        changes.append(
            f"Typ obsahu: {before_content.label} → "
            f"{labels.get(normalized_content, after_content_label)}"
        )
        video = session.get(Video, video_id)

    raw_part = media_part_number.strip()
    try:
        part = int(raw_part) if raw_part else None
    except ValueError as exc:
        raise ValueError(MEDIA_PART_NUMBER_ERROR) from exc
    if part != video.media_part_number:
        before = video.media_part_number
        set_media_part_number(video, part)
        changes.append(
            f"Část média: {before or 'neurčeno'} → {part or 'neurčeno'}"
        )

    raw_episode = manual_episode_number.strip()
    if raw_episode != before_episode:
        apply_episode_position_edit(session, video_id, raw_episode)
        label = hierarchy_number_field_label(
            effective_video_content_display(video).value
        )
        changes.append(
            f"{label}: {before_episode or 'automaticky'} → "
            f"{raw_episode or 'automaticky'}"
        )
    if not changes:
        raise ValueError("Formulář neobsahuje žádnou změněnou hierarchy hodnotu.")
    result_identity = catalog_video_identity(video, title)
    return [
        f"Současná effective identita: {current_identity}",
        *changes,
        f"Výsledná effective identita: {result_identity}",
    ]


_FIELDS = {
    "variant_group": {
        "catalog_title_id", "manual_label", "release_source",
        "content_variant", "note",
    },
    "recap_position": {"manual_episode_number"},
    "title_hierarchy": {
        "part_type_manual", "season_number_manual", "season_label_manual",
        "part_number_manual", "sort_order_manual", "hierarchy_verified",
        "numbering_mode", "episode_start_offset",
    },
    "video_hierarchy": {
        "catalog_title_id", "content_type", "manual_episode_number",
        "media_part_number",
    },
}


def _validate_string_values(values: dict, keys: set[str]) -> None:
    if any(type(values[key]) is not str for key in keys):
        raise ValueError("Edits obsahují neplatný typ hodnoty.")


def _validated_sections(payload: object) -> list[tuple[str, int, dict]]:
    if not isinstance(payload, dict) or set(payload) != {"sections"}:
        raise ValueError("Neplatný obsah hromadného uložení.")
    sections = payload["sections"]
    if not isinstance(sections, list) or not sections:
        raise ValueError("Vyberte alespoň jednu upravenou sekci.")
    validated = []
    seen = set()
    for section in sections:
        if not isinstance(section, dict) or set(section) != {"kind", "id", "values"}:
            raise ValueError("Neplatná editační sekce.")
        kind, target_id, values = section["kind"], section["id"], section["values"]
        if type(kind) is not str or kind not in _FIELDS or type(target_id) is not int or target_id < 1:
            raise ValueError("Nepodporovaná editační sekce.")
        if not isinstance(values, dict):
            raise ValueError("Neplatná pole editační sekce.")
        if kind == "title_hierarchy":
            # The same contract as the local title form: only conditional
            # inputs unused by the submitted part type may be omitted.
            valid_keys = (
                set(values) <= _FIELDS[kind]
                and "hierarchy_verified" in values
                and not missing_title_hierarchy_fields(
                    values.get("part_type_manual"), values,
                )
            )
        else:
            valid_keys = set(values) == _FIELDS[kind]
        if not valid_keys:
            raise ValueError("Neplatná pole editační sekce.")
        if (kind, target_id) in seen:
            raise ValueError("Stejná sekce je v dávce vícekrát.")
        seen.add((kind, target_id))
        if kind in {"variant_group", "video_hierarchy"}:
            if type(values["catalog_title_id"]) is not int or values["catalog_title_id"] < 1:
                raise ValueError("Neplatná cílová část editační sekce.")
        if kind == "variant_group":
            _validate_string_values(values, _FIELDS[kind] - {"catalog_title_id"})
        elif kind == "recap_position":
            _validate_string_values(values, _FIELDS[kind])
        elif kind == "title_hierarchy":
            if type(values["hierarchy_verified"]) is not bool:
                raise ValueError("Neplatný stav ověření části.")
            _validate_string_values(values, set(values) - {"hierarchy_verified"})
        else:
            _validate_string_values(values, _FIELDS[kind] - {"catalog_title_id"})
        validated.append((kind, target_id, values))
    return validated


def apply_hierarchy_page_edits(
    session: Session, collection_id: int, payload: object,
) -> HierarchyPageEditResult:
    """Stage only whitelisted edits; the caller owns the single commit."""
    validated = _validated_sections(payload)
    groups: list[EditChangeGroup] = []
    for kind, target_id, values in validated:
        try:
            if kind == "variant_group":
                group = session.get(VideoVariantGroup, target_id)
                if group is None:
                    raise MissingEditTarget("Variant group už neexistuje.")
                before_values = (
                    group.manual_label, group.release_source or "",
                    group.content_variant or "", group.note or "",
                )
                after_values = (
                    values["manual_label"], values["release_source"],
                    values["content_variant"], values["note"],
                )
                labels = ("Název varianty", "Zdroj", "Obsahová varianta", "Poznámka")
                changes = tuple(
                    f"{label}: {_human_value(before)} → {_human_value(after)}"
                    for label, before, after in zip(
                        labels, before_values, after_values, strict=True,
                    )
                    if before != after
                )
                if not changes:
                    raise ValueError("Variant group neobsahuje žádnou změněnou hodnotu.")
                update_video_variant_group_for_title(
                    session, collection_id, values["catalog_title_id"], target_id,
                    manual_label=values["manual_label"],
                    release_source=values["release_source"] or None,
                    content_variant=values["content_variant"] or None,
                    note=values["note"] or None,
                )
                groups.append(EditChangeGroup(
                    f"Varianta: {before_values[0]}", changes,
                ))
            elif kind == "recap_position":
                video = session.get(Video, target_id)
                if video is None:
                    raise MissingEditTarget("Video už neexistuje.")
                before = str(manual_episode_number_input_value(video) or "automaticky")
                apply_episode_position_edit(
                    session, target_id, values["manual_episode_number"],
                    expected_collection_id=collection_id, recap_only=True,
                )
                after = catalog_video_identity(video, video.catalog_title)
                groups.append(EditChangeGroup(video.filename, (
                    f"Ruční pozice Recapu: {before} → {values['manual_episode_number']}",
                    f"Výsledná effective identita: {after}",
                )))
            elif kind == "title_hierarchy":
                groups.append(apply_title_hierarchy_form_edit(
                    session, collection_id, target_id,
                    **{field: values.get(field) for field in _FIELDS[kind]},
                ))
            else:
                video = session.get(Video, target_id)
                filename = video.filename if video is not None else f"Video #{target_id}"
                changes = apply_title_video_hierarchy_edit(
                    session, collection_id, values["catalog_title_id"], target_id,
                    manual_episode_number=values["manual_episode_number"],
                    media_part_number=values["media_part_number"],
                    content_type=values["content_type"],
                )
                groups.append(EditChangeGroup(filename, tuple(changes)))
        except (MissingEditTarget, ValueError) as exc:
            target = (
                session.get(Video, target_id).filename
                if kind in {"recap_position", "video_hierarchy"}
                and session.get(Video, target_id) is not None
                else f"sekce {kind} #{target_id}"
            )
            raise ValueError(
                f"Změnu „{target}“ nelze uložit: {exc} "
                "Žádná část dávky nebyla uložena. Opravte tuto sekci a "
                "zobrazte náhled znovu."
            ) from exc
    return HierarchyPageEditResult(len(validated), tuple(groups))
