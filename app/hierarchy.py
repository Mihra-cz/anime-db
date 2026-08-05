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
ROMAN_SUFFIX = re.compile(r"^(.*\S)\s+([IVXLCDM]+)$", re.IGNORECASE)
ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5}


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


def parse_explicit_part(name: str) -> tuple[str, int | None, str | None] | None:
    stripped = name.strip()
    if match := NUMBERED_PART.fullmatch(stripped):
        number = int(match.group(1) or match.group(2) or ORDINALS.get(
            (match.group(3) or "").casefold(), 0
        ) or match.group(5))
        kind = (match.group(4) or "season").casefold()
        return kind, number, f"S{number}"
    folded = stripped.casefold()
    if folded in {"ova", "oad"}:
        return "ova", None, "OVA"
    if folded in {"special", "specials"}:
        return "special", None, "Specials"
    return None


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
        part_data = None
        for index in range(offset + 1, len(directories)):
            name = directories[index]
            if explicit := parse_explicit_part(name):
                part_index, part_data = index, explicit
                break
            key = (tuple(directories[:index]), name)
            if key in roman_parts:
                match = ROMAN_SUFFIX.fullmatch(name.strip())
                number = roman_to_int(match.group(2)) if match else None
                part_index, part_data = index, ("season", number, f"S{number}")
                break
        if part_index is None:
            legacy = determine_parent_series(path)
            collection = CollectionIdentity(legacy.name, legacy.relative_path)
            title = TitleIdentity(
                legacy.name, legacy.relative_path, "title", None, None, None, 0,
                normalize_title(legacy.name), "direct_title_or_no_safe_part_context",
            )
        else:
            collection_dirs = directories[:part_index]
            collection = CollectionIdentity(
                collection_dirs[-1], PurePosixPath(*collection_dirs).as_posix()
            )
            part_name = directories[part_index]
            kind, number, label = part_data
            roman_match = ROMAN_SUFFIX.fullmatch(part_name.strip())
            title = TitleIdentity(
                part_name, PurePosixPath(*directories[:part_index + 1]).as_posix(),
                kind, number, label, part_name, number or 0,
                normalize_title(roman_match.group(1) if roman_match else collection.local_title),
                "roman_sibling_same_base" if roman_match else "explicit_part_pattern",
            )
        result[path] = HierarchyIdentity(collection, title)
    return result
