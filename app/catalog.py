from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath
import re

from .models import Video

LANGUAGE_ALIASES = {
    "cs": "cs", "cze": "cs", "ces": "cs", "czech": "cs", "čeština": "cs",
    "sk": "sk", "slk": "sk", "slo": "sk", "slovak": "sk", "slovenčina": "sk",
    "en": "eng", "eng": "eng", "english": "eng",
    "de": "deu", "deu": "deu", "ger": "deu", "german": "deu",
    "fr": "fra", "fra": "fra", "fre": "fra", "french": "fra",
    "es": "spa", "spa": "spa", "spanish": "spa",
    "it": "ita", "ita": "ita", "italian": "ita",
    "ja": "jpn", "jpn": "jpn", "japanese": "jpn",
    "ko": "kor", "kor": "kor", "korean": "kor",
    "zh": "zho", "zho": "zho", "chi": "zho", "chinese": "zho",
    "pl": "pol", "pol": "pol", "polish": "pol",
    "ru": "rus", "rus": "rus", "russian": "rus",
    "uk": "ukr", "ukr": "ukr", "ukrainian": "ukr",
    "pt": "por", "por": "por", "portuguese": "por",
    "hu": "hun", "hun": "hun", "hungarian": "hun",
}


def normalize_language(language: str | None, title: str | None = None) -> str:
    raw = (language or "").strip().casefold().replace("_", "-")
    raw = raw.split("-", 1)[0]
    if raw and raw not in {"und", "unk", "unknown", "n/a", "none"}:
        normalized = LANGUAGE_ALIASES.get(raw)
        if normalized:
            return normalized

    title_text = (title or "").casefold()
    for word, normalized in (("english", "eng"), ("czech", "cs"), ("slovak", "sk")):
        if word in title_text:
            return normalized
    return "unknown"


def classify_video(relative_path: str) -> str:
    value = relative_path.casefold()
    tokens = re.sub(r"[^a-z0-9]+", " ", value).split()
    token_set = set(tokens)
    compact = re.sub(r"[^a-z0-9]+", "", value)
    if "ncop" in compact or "creditlessopening" in compact:
        return "ncop"
    if "nced" in compact or "creditlessending" in compact:
        return "nced"
    if "ova" in token_set or "oad" in token_set:
        return "ova"
    if "special" in token_set or "specials" in token_set or "sp" in token_set:
        return "special"
    if "pv" in token_set or "preview" in token_set or "trailer" in token_set:
        return "pv"
    if "cm" in token_set or "commercial" in token_set:
        return "cm"
    if "menu" in token_set:
        return "menu"
    filename = value.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    if "episode" in token_set or re.search(r"(?:^|[^a-z0-9])(?:s\d{1,2}e)?\d{1,4}(?:v\d+)?(?:[^a-z0-9]|$)", filename):
        return "episode"
    return "other"


@dataclass(frozen=True)
class TranslationStatus:
    has_cs: bool
    has_sk: bool
    has_cs_or_sk: bool
    subtitle_source: str | None
    has_unknown: bool
    automatic_has_cs: bool
    automatic_has_sk: bool


def translation_status(video: Video) -> TranslationStatus:
    internal = {track.normalized_language for track in video.internal_subtitles}
    external = {track.normalized_language for track in video.external_subtitles}
    target = {"cs", "sk"}
    internal_target = bool(internal & target)
    external_target = bool(external & target)
    source = "both" if internal_target and external_target else "internal" if internal_target else "external" if external_target else None
    automatic_has_cs = "cs" in internal or "cs" in external
    automatic_has_sk = "sk" in internal or "sk" in external
    has_cs = automatic_has_cs or bool(video.manual_hardsub_cs)
    has_sk = automatic_has_sk or bool(video.manual_hardsub_sk)
    return TranslationStatus(
        has_cs=has_cs,
        has_sk=has_sk,
        has_cs_or_sk=has_cs or has_sk,
        subtitle_source=source,
        has_unknown="unknown" in internal or "unknown" in external,
        automatic_has_cs=automatic_has_cs,
        automatic_has_sk=automatic_has_sk,
    )


GENERIC_ROOTS = {"anime", "library", "media", "videos", "video"}
STRUCTURAL_DIRECTORY = re.compile(
    r"^(?:(?:s[ée]rie|series|season|cour|part)\s*[-_. ]*\d+|s\s*[-_. ]*\d+)"
    r"(?:\s*\([^)]*\))?$"
    r"|^(?:specials?|extras?|bonus|ova|oad)(?:\s*\([^)]*\))?$",
    re.IGNORECASE,
)


def is_technical_series_directory(name: str) -> bool:
    return STRUCTURAL_DIRECTORY.fullmatch(name.strip()) is not None


@dataclass(frozen=True)
class SeriesIdentity:
    name: str
    relative_path: str


def determine_parent_series(relative_path: str) -> SeriesIdentity:
    all_directories = list(PurePosixPath(relative_path).parts[:-1])
    first_meaningful = 0
    while (
        first_meaningful < len(all_directories)
        and all_directories[first_meaningful].casefold() in GENERIC_ROOTS
    ):
        first_meaningful += 1
    directories = all_directories[first_meaningful:]
    if not directories:
        return SeriesIdentity("Knihovna", ".")

    structural_index = next(
        (index for index, name in enumerate(directories) if is_technical_series_directory(name)),
        None,
    )
    if structural_index is not None:
        actual_series_index = first_meaningful + structural_index - 1
        while (
            actual_series_index >= 0
            and is_technical_series_directory(all_directories[actual_series_index])
        ):
            actual_series_index -= 1
        if actual_series_index < 0:
            return SeriesIdentity("Knihovna", ".")
    else:
        actual_series_index = len(all_directories) - 1
    selected = all_directories[:actual_series_index + 1]
    return SeriesIdentity(all_directories[actual_series_index], PurePosixPath(*selected).as_posix())


@dataclass
class SeriesSummary:
    name: str
    relative_path: str
    total: int = 0
    problematic: int = 0
    unknown: int = 0

    @property
    def translated(self) -> int:
        return self.total - self.problematic


def group_videos_by_series(
    videos: Iterable[Video], is_problematic: Callable[[Video], bool]
) -> list[SeriesSummary]:
    groups: dict[str, SeriesSummary] = {}
    for video in videos:
        identity = determine_parent_series(video.relative_path)
        summary = groups.setdefault(
            identity.relative_path,
            SeriesSummary(identity.name, identity.relative_path),
        )
        summary.total += 1
        summary.problematic += is_problematic(video)
        summary.unknown += translation_status(video).has_unknown
    return sorted(
        (summary for summary in groups.values() if summary.problematic),
        key=lambda summary: (-summary.problematic, summary.name.casefold()),
    )


def set_manual_hardsub(
    video: Video, mode: str, *, verified_at: datetime | None = None
) -> None:
    values = {
        "none": (False, False),
        "cs": (True, False),
        "sk": (False, True),
        "both": (True, True),
    }
    if mode not in values:
        raise ValueError("Neplatná hodnota ručního hardsubu")
    video.manual_hardsub_cs, video.manual_hardsub_sk = values[mode]
    video.manual_hardsub_verified_at = (
        (verified_at or datetime.now(timezone.utc)) if mode != "none" else None
    )
