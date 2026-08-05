from __future__ import annotations

from collections.abc import Iterable
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
    episodes: int = 0
    bonus: int = 0
    cs: int = 0
    sk: int = 0
    missing: int = 0
    matched: int = 0

    @property
    def translated(self) -> int:
        return self.total - self.problematic


@dataclass
class CatalogResults:
    groups: list[SeriesSummary]
    videos_by_title: dict[str, list[Video]]
    query: str

    @property
    def video_count(self) -> int:
        return sum(len(videos) for videos in self.videos_by_title.values())


FILTER_LABELS = {
    "all": "Všechna videa",
    "only-cs": "Pouze CZ",
    "only-sk": "Pouze SK",
    "both": "CZ i SK",
    "missing": "Bez CZ/SK",
    "unknown": "Neznámé titulky",
    "episodes": "Běžné epizody",
    "bonus": "Bonusová / ostatní videa",
    "type-special": "Specials",
    "type-ova": "OVA",
    "type-ncop": "NCOP",
    "type-nced": "NCED",
    "type-pv": "PV",
    "type-cm": "CM",
    "type-menu": "Menu",
    "type-other": "Ostatní",
}


def video_matches_filter(video: Video, filter_name: str) -> bool:
    status = translation_status(video)
    predicates = {
        "all": True,
        "only-cs": status.has_cs and not status.has_sk,
        "only-sk": status.has_sk and not status.has_cs,
        "both": status.has_cs and status.has_sk,
        "missing": not status.has_cs_or_sk,
        "unknown": status.has_unknown,
        "episodes": video.file_type == "episode",
        "bonus": video.file_type != "episode",
    }
    if filter_name.startswith("type-"):
        return video.file_type == filter_name.removeprefix("type-")
    if filter_name not in predicates:
        raise ValueError(f"Neznámý filtr: {filter_name}")
    return predicates[filter_name]


def group_videos_by_series(
    videos: Iterable[Video], filter_name: str
) -> list[SeriesSummary]:
    return build_catalog_results(videos, filter_name).groups


def normalize_search_query(query: str | None) -> str:
    return (query or "").strip()[:200]


def _contains_query(value: str | None, query: str) -> bool:
    return query in (value or "").casefold()


def video_matches_search(video: Video, query: str) -> bool:
    if not query:
        return True
    season = derive_season_info(video.relative_path)
    episode = derive_episode_number(video.filename)
    values = (
        video.filename,
        video.relative_path,
        video.file_type,
        season.label,
        season.original,
        str(episode) if episode is not None else None,
    )
    return any(_contains_query(value, query) for value in values)


def build_catalog_results(
    videos: Iterable[Video], filter_name: str, query: str | None = None
) -> CatalogResults:
    query_text = normalize_search_query(query)
    folded_query = query_text.casefold()
    groups: dict[str, SeriesSummary] = {}
    all_by_title: dict[str, list[Video]] = {}
    for video in list(videos):
        identity = determine_parent_series(video.relative_path)
        all_by_title.setdefault(identity.relative_path, []).append(video)
        summary = groups.setdefault(
            identity.relative_path,
            SeriesSummary(identity.name, identity.relative_path),
        )
        summary.total += 1
        status = translation_status(video)
        filter_match = video_matches_filter(video, filter_name)
        summary.problematic += filter_match
        summary.episodes += video.file_type == "episode"
        summary.bonus += video.file_type != "episode"
        summary.cs += status.has_cs
        summary.sk += status.has_sk
        summary.missing += not status.has_cs_or_sk
        summary.unknown += status.has_unknown
    matches_by_title: dict[str, list[Video]] = {}
    for title_path, title_videos_list in all_by_title.items():
        identity = determine_parent_series(title_videos_list[0].relative_path)
        filtered = [video for video in title_videos_list if video_matches_filter(video, filter_name)]
        title_matches = bool(folded_query) and (
            _contains_query(identity.name, folded_query)
            or _contains_query(identity.relative_path, folded_query)
        )
        matched_videos = filtered if not folded_query or title_matches else [
            video for video in filtered if video_matches_search(video, folded_query)
        ]
        if matched_videos:
            matches_by_title[title_path] = sorted(matched_videos, key=video_sort_key)
            groups[title_path].matched = len(matched_videos)

    ordered_groups = sorted(
        (groups[path] for path in matches_by_title),
        key=lambda summary: (-summary.matched, summary.name.casefold()),
    )
    return CatalogResults(ordered_groups, matches_by_title, query_text)


@dataclass(frozen=True)
class SeasonInfo:
    label: str | None
    original: str | None


SEASON_NUMBER = re.compile(
    r"^(?:s[ée]rie|series|season)\s*[-_. ]*0*(\d+)(?:\s*\([^)]*\))?$",
    re.IGNORECASE,
)
SHORT_SEASON = re.compile(r"^s\s*[-_. ]*0*(\d+)(?:\s*\([^)]*\))?$", re.IGNORECASE)
COUR_PART = re.compile(r"^(cour|part)\s*[-_. ]*0*(\d+)(?:\s*\([^)]*\))?$", re.IGNORECASE)


def derive_season_info(relative_path: str) -> SeasonInfo:
    for directory in PurePosixPath(relative_path).parts[:-1]:
        if match := SEASON_NUMBER.fullmatch(directory.strip()):
            return SeasonInfo(f"S{int(match.group(1))}", directory)
        if match := SHORT_SEASON.fullmatch(directory.strip()):
            return SeasonInfo(f"S{int(match.group(1))}", directory)
        if match := COUR_PART.fullmatch(directory.strip()):
            return SeasonInfo(f"{match.group(1).title()} {int(match.group(2))}", directory)
        if directory.strip().casefold() in {"special", "specials"}:
            return SeasonInfo("Specials", directory)
        if directory.strip().casefold() in {"ova", "oad"}:
            return SeasonInfo("OVA", directory)
    return SeasonInfo(None, None)


EXPLICIT_EPISODE = re.compile(
    r"(?:^|[^a-z0-9])(?:episode|ep|e)\s*[-_. ]*0*(\d{1,3})(?:v\d+)?(?:[^a-z0-9]|$)",
    re.IGNORECASE,
)
BARE_EPISODE = re.compile(r"(?:^|[^a-z0-9])0*(\d{1,3})(?:v\d+)?(?:[^a-z0-9]|$)", re.IGNORECASE)


def derive_episode_number(filename: str) -> int | None:
    stem = PurePosixPath(filename).stem
    if match := EXPLICIT_EPISODE.search(stem):
        return int(match.group(1))
    candidates = {int(value) for value in BARE_EPISODE.findall(stem)}
    candidates = {value for value in candidates if value not in {720} and value < 190}
    return candidates.pop() if len(candidates) == 1 else None


TYPE_ORDER = {"episode": 0, "special": 1, "ova": 2, "ncop": 3, "nced": 4, "pv": 5, "cm": 6, "menu": 7, "other": 8}


def video_sort_key(video: Video):
    season = derive_season_info(video.relative_path).label
    if season and (match := re.fullmatch(r"S(\d+)", season)):
        season_key = (0, int(match.group(1)), "")
    elif season is None:
        season_key = (1, 0, "")
    else:
        season_key = (2, 0, season.casefold())
    episode = derive_episode_number(video.filename)
    return (
        season_key,
        episode is None,
        episode if episode is not None else 0,
        TYPE_ORDER.get(video.file_type, 99),
        video.filename.casefold(),
    )


def title_videos(videos: Iterable[Video], title_path: str) -> list[Video]:
    return sorted(
        (video for video in videos if determine_parent_series(video.relative_path).relative_path == title_path),
        key=video_sort_key,
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
