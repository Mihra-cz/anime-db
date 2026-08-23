from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import PurePosixPath
import re

from .catalog import GENERIC_ROOTS, determine_parent_series, normalize_title


@dataclass(frozen=True)
class CollectionIdentity:
    local_title: str
    relative_root_path: str


@dataclass(frozen=True)
class TitleIdentity:
    local_title: str
    relative_root_path: str
    part_type: str
    season_number: int | None
    part_number: int | None
    season_label: str | None
    original_folder_name: str | None
    sort_order: int
    normalized_base: str
    detection_reason: str


@dataclass(frozen=True)
class HierarchyIdentity:
    collection: CollectionIdentity
    title: TitleIdentity


NUMBERED_PART = re.compile(
    r"^(?:(?:serie|s[ée]rie|series|season|s)\s*[-_. ]*0*(\d+)"
    r"|(\d+)(?:st|nd|rd|th)\s+season"
    r"|(first|second|third|fourth|fifth)\s+season"
    r"|(part|cour)\s*[-_. ]*0*(\d+))"
    r"(?:\s*\([^)]*\))?$",
    re.IGNORECASE,
)
SEASON_AND_PART = re.compile(
    r"^(?:(?:serie|s[ée]rie|series|season|s)\s*[-_. ]*0*(\d+)"
    r"|(\d+)(?:st|nd|rd|th)\s+season"
    r"|(first|second|third|fourth|fifth)\s+season)"
    r"\s+part\s*[-_. ]*0*(\d+)"
    r"(?:\s*\([^)]*\))?$",
    re.IGNORECASE,
)
SEASON_SCOPED_SUPPLEMENTARY = re.compile(
    r"^(?:(?:serie|s[ée]rie|series|season|s)\s*[-_. ]*0*(\d+)"
    r"|(\d+)(?:st|nd|rd|th)\s+season"
    r"|(first|second|third|fourth|fifth)\s+season)"
    r"\s+(.+)$",
    re.IGNORECASE,
)
ROMAN_SUFFIX = re.compile(r"^(.*\S)\s+([IVXLCDM]+)$", re.IGNORECASE)
ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5}
ANNOTATION_SUFFIX = re.compile(r"\s*\([^)]*\)\s*$")
INTERNAL_CODE_SUFFIX = re.compile(
    r"\s+[A-Z]\d{2}(?:-[A-Z]\d{2})?\s*$", re.IGNORECASE
)
SUPPLEMENTARY_PARTS = {
    "bonus": ("bonus", None, "Bonus"),
    "bonuses": ("bonus", None, "Bonus"),
    "extra": ("bonus", None, "Bonus"),
    "extras": ("bonus", None, "Bonus"),
    "nc": ("bonus", None, "NC"),
    "ncop": ("bonus", None, "NCOP"),
    "nced": ("bonus", None, "NCED"),
    "op": ("bonus", None, "OP"),
    "ed": ("bonus", None, "ED"),
    "preview": ("preview", None, "Preview"),
    "previews": ("preview", None, "Preview"),
    "pv": ("preview", None, "Preview"),
    "recap": ("recap", None, "Recap"),
    "recaps": ("recap", None, "Recap"),
    "movie": ("film", None, "Movies"),
    "movies": ("film", None, "Movies"),
    "film": ("film", None, "Movies"),
    "films": ("film", None, "Movies"),
    "short": ("bonus", None, "Shorts"),
    "shorts": ("bonus", None, "Shorts"),
    "sp": ("special", None, "Specials"),
    "sps": ("special", None, "Specials"),
}


def roman_to_int(value: str) -> int | None:
    values = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    text = value.upper()
    if not text or any(character not in values for character in text):
        return None
    total, previous = 0, 0
    for character in reversed(text):
        current = values[character]
        total += -current if current < previous else current
        previous = max(previous, current)
    canonical = _int_to_roman(total)
    return total if canonical == text and 0 < total <= 50 else None


def _int_to_roman(number: int) -> str:
    result = []
    for value, token in ((10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")):
        while number >= value:
            result.append(token)
            number -= value
    return "".join(result)


def _supplementary_part(value: str) -> tuple[str, int | None, str | None] | None:
    folded = value.strip(" -_.").casefold()
    if folded in {"ova", "oad"}:
        return "ova", None, "OVA"
    if folded in {"special", "specials"}:
        return "special", None, "Specials"
    compact = re.sub(r"[^a-z0-9]+", "", folded)
    if compact in {"cmpv", "commercialspv", "commercialsandpv"}:
        return "bonus", None, "Bonus"
    return SUPPLEMENTARY_PARTS.get(folded)


def parse_explicit_part(
    name: str,
) -> tuple[str, int | None, int | None, str | None] | None:
    stripped = _name_without_annotation(name)
    if match := SEASON_SCOPED_SUPPLEMENTARY.fullmatch(stripped):
        number = int(match.group(1) or match.group(2) or ORDINALS[
            match.group(3).casefold()
        ])
        if supplementary := _supplementary_part(match.group(4)):
            return supplementary[0], number, None, f"S{number}"
    if match := SEASON_AND_PART.fullmatch(stripped):
        season_number = int(match.group(1) or match.group(2) or ORDINALS[
            match.group(3).casefold()
        ])
        return "part", season_number, int(match.group(4)), f"S{season_number}"
    if match := NUMBERED_PART.fullmatch(stripped):
        number = int(match.group(1) or match.group(2) or ORDINALS.get(
            (match.group(3) or "").casefold(), 0
        ) or match.group(5))
        kind = (match.group(4) or "season").casefold()
        if kind in {"part", "cour"}:
            return kind, None, number, None
        return kind, number, None, f"S{number}"
    supplementary = _supplementary_part(stripped)
    return (
        (supplementary[0], supplementary[1], None, supplementary[2])
        if supplementary else None
    )


def _name_without_annotation(name: str) -> str:
    value = name.strip()
    while value:
        without_parentheses = ANNOTATION_SUFFIX.sub("", value).strip()
        without_code = INTERNAL_CODE_SUFFIX.sub("", without_parentheses).strip()
        if without_code == value:
            return value
        value = without_code
    return value


def _is_related_named_child(parent_name: str, child_name: str) -> bool:
    """Silný ancestry hint: child opakuje celý základ bez interního suffixu parentu."""
    parent = normalize_title(_name_without_annotation(parent_name))
    child = normalize_title(_name_without_annotation(child_name))
    return bool(parent and child and child != parent and child.startswith(f"{parent} "))


def _meaningful_directories(relative_path: str) -> tuple[list[str], int]:
    directories = list(PurePosixPath(relative_path).parts[:-1])
    offset = 0
    while offset < len(directories) and directories[offset].casefold() in GENERIC_ROOTS:
        offset += 1
    return directories, offset


def derive_library_hierarchy(relative_paths: list[str]) -> dict[str, HierarchyIdentity]:
    sibling_names: dict[tuple[str, ...], set[str]] = defaultdict(set)
    parsed: dict[str, tuple[list[str], int]] = {}
    for path in relative_paths:
        directories, offset = _meaningful_directories(path)
        parsed[path] = directories, offset
        for index in range(offset, len(directories)):
            sibling_names[tuple(directories[:index])].add(directories[index])

    roman_parts: set[tuple[tuple[str, ...], str]] = set()
    for parent, names in sibling_names.items():
        groups: dict[str, list[tuple[str, int]]] = defaultdict(list)
        for name in names:
            match = ROMAN_SUFFIX.fullmatch(name.strip())
            number = roman_to_int(match.group(2)) if match else None
            if match and number:
                groups[normalize_title(match.group(1))].append((name, number))
        for values in groups.values():
            if len({number for _, number in values}) >= 2:
                roman_parts.update((parent, name) for name, _ in values)

    result: dict[str, HierarchyIdentity] = {}
    for path, (directories, offset) in parsed.items():
        part_index = None
        title_index = None
        part_data = None
        season_scope = None
        for index in range(offset + 1, len(directories)):
            name = directories[index]
            if explicit := parse_explicit_part(name):
                kind, season_number, part_number, label = explicit
                if kind == "season":
                    season_scope = season_number
                elif season_number is None and season_scope is not None:
                    season_number = season_scope
                    label = f"S{season_scope}"
                explicit = kind, season_number, part_number, label
                if part_index is None:
                    part_index = index
                title_index, part_data = index, explicit
                continue
            key = (tuple(directories[:index]), name)
            if key in roman_parts:
                match = ROMAN_SUFFIX.fullmatch(name.strip())
                number = roman_to_int(match.group(2)) if match else None
                if part_index is None:
                    part_index = index
                season_scope = number
                title_index, part_data = index, ("season", number, None, f"S{number}")
                continue
            if (
                part_index is None
                and index == offset + 1
                and _is_related_named_child(directories[index - 1], name)
            ):
                part_index = title_index = index
                part_data = ("title", None, None, None)
        if part_index is None:
            legacy = determine_parent_series(path)
            collection = CollectionIdentity(legacy.name, legacy.relative_path)
            direct_type = (
                "film"
                if re.search(r"\((?:film|movie)\)\s*$", legacy.name, re.IGNORECASE)
                else "title"
            )
            title = TitleIdentity(
                legacy.name, legacy.relative_path, direct_type, None, None,
                "Film" if direct_type == "film" else None, None, 0,
                normalize_title(legacy.name),
                "direct_film_root" if direct_type == "film"
                else "direct_title_or_no_safe_part_context",
            )
        else:
            collection_dirs = directories[:part_index]
            collection = CollectionIdentity(
                collection_dirs[-1], PurePosixPath(*collection_dirs).as_posix()
            )
            assert title_index is not None
            part_name = directories[title_index]
            kind, season_number, part_number, label = part_data
            supplementary_named_child = (
                kind in {"film", "ova", "special", "preview", "recap", "bonus"}
                and title_index == part_index
                and len(directories) > part_index + 1
            )
            if supplementary_named_child:
                title_index = part_index + 1
                part_name = f"{directories[part_index]} – {directories[title_index]}"
            roman_match = ROMAN_SUFFIX.fullmatch(part_name.strip())
            title = TitleIdentity(
                part_name, PurePosixPath(*directories[:title_index + 1]).as_posix(),
                kind, season_number, part_number, label, part_name,
                (
                    (season_number * 1000 + part_number)
                    if season_number is not None and part_number is not None
                    else part_number or season_number or 0
                ),
                normalize_title(roman_match.group(1) if roman_match else collection.local_title),
                "roman_sibling_same_base" if roman_match else
                "related_named_child" if kind == "title" else
                "supplementary_named_child" if supplementary_named_child else
                "explicit_part_pattern",
            )
        result[path] = HierarchyIdentity(collection, title)
    return result
