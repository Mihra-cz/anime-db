from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import PurePosixPath
import re
import unicodedata

from .models import CatalogCollection, CatalogTitle, Video

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


@dataclass(frozen=True)
class SubtitleTrackDisplay:
    label: str
    details: str


SUBTITLE_LANGUAGE_LABELS = {
    "cs": "CZ", "sk": "SK", "eng": "EN", "deu": "DE", "fra": "FR",
    "spa": "ES", "ita": "IT", "jpn": "JA", "kor": "KO", "zho": "ZH",
    "pol": "PL", "rus": "RU", "ukr": "UK", "por": "PT", "hun": "HU",
    "unknown": "?",
}

ROOT_FOLDER = "."
ROOT_VIDEO_GROUP_LABEL = "Nezařazená videa z kořene knihovny"


def catalog_title_display_title(title: CatalogTitle) -> str:
    """Vrátí nejlepší uložený uživatelský název bez změny lokální identity."""
    return (
        title.manual_display_title
        or (title.metadata_record.display_title if title.metadata_record else None)
        or title.local_title
    )


def catalog_collection_display_title(collection: CatalogCollection) -> str:
    """Vrátí uživatelský název kolekce bez změny její logické identity."""
    return collection.manual_display_title or collection.local_title


def catalog_title_series_label(title: CatalogTitle) -> str:
    """Vrátí popisek části výhradně z hierarchie CatalogTitle."""
    if title.effective_season_label:
        return title.effective_season_label
    if title.effective_part_type == "season" and title.effective_season_number is not None:
        return f"S{title.effective_season_number}"
    if title.effective_part_type in {"part", "cour"} and title.part_number is not None:
        prefix = "Part" if title.effective_part_type == "part" else "Cour"
        return f"{prefix} {title.part_number}"
    return {
        "film": "Film", "ova": "OVA", "special": "Special",
        "migration_review": "Kontrola migrace",
    }.get(title.effective_part_type, "—")


def subtitle_track_display(video: Video) -> list[SubtitleTrackDisplay]:
    """Sloučí interní a externí subtitle tracky do unikátních čitelných položek."""
    grouped: dict[tuple[str, str], list[str]] = {}
    tracks = [
        ("interní", track) for track in video.internal_subtitles
    ] + [
        ("externí", track) for track in video.external_subtitles
    ]
    for source, track in tracks:
        language = SUBTITLE_LANGUAGE_LABELS.get(
            track.normalized_language, track.normalized_language.upper()
        )
        codec = (track.codec or "").strip().upper()
        key = (language, codec)
        raw_language = (track.language or "unknown").strip() or "unknown"
        detail = f"{source}: raw={raw_language}, codec={track.codec or '?'}"
        if source == "interní" and getattr(track, "title", None):
            detail += f", název={track.title}"
        if source == "externí":
            detail += f", cesta={track.relative_path}"
        grouped.setdefault(key, []).append(detail)
    return [
        SubtitleTrackDisplay(
            label=f"{language} ({codec})" if codec else language,
            details="; ".join(details),
        )
        for (language, codec), details in grouped.items()
    ]


def is_root_video(video: Video) -> bool:
    return len(PurePosixPath(video.relative_path).parts) == 1


def meaningful_root_collection(video: Video) -> CatalogCollection | None:
    if not is_root_video(video):
        return None
    candidates = (
        video.catalog_title.collection if video.catalog_title else None,
        video.catalog_collection,
    )
    return next(
        (collection for collection in candidates if collection and collection.relative_root_path != ROOT_FOLDER),
        None,
    )


def has_meaningful_root_assignment(video: Video) -> bool:
    return meaningful_root_collection(video) is not None


def manual_hardsub_state(video: Video) -> str:
    if video.manual_hardsub_verified_at is None:
        return "unknown"
    return "yes" if video.manual_hardsub_cs or video.manual_hardsub_sk else "no"


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


def normalize_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    return " ".join(
        re.sub(r"[^a-z0-9]+", " ", "".join(
            character for character in normalized if not unicodedata.combining(character)
        )).split()
    )


@dataclass
class SeriesSummary:
    name: str
    relative_path: str
    catalog_title_id: int | None = None
    catalog_collection_id: int | None = None
    metadata_status: str = "unlinked"
    is_root_group: bool = False
    part_ids: set[int] = field(default_factory=set)
    linked_part_ids: set[int] = field(default_factory=set)
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
        return self.total - self.missing

    @property
    def parts(self) -> int:
        return len(self.part_ids) or 1

    @property
    def linked_parts(self) -> int:
        return len(self.linked_part_ids)


@dataclass
class CatalogResults:
    groups: list[SeriesSummary]
    videos_by_title: dict[str, list[Video]]
    query: str
    sort: str
    direction: str

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
    "unassigned": "Nezařazená videa",
    "hierarchy-conflict": "Konflikt hierarchie",
    "hierarchy-review": "Hierarchie ke kontrole",
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
        "unassigned": video.catalog_title_id is None,
        "hierarchy-conflict": bool(
            video.catalog_collection
            and video.catalog_collection.hierarchy_status == "conflict"
        ),
        "hierarchy-review": bool(
            video.catalog_collection
            and (
                video.catalog_collection.hierarchy_status == "review_required"
                or video.season_episode_number is None
            )
        ),
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


def natural_sort_key(value: str):
    normalized = unicodedata.normalize("NFKD", value.casefold())
    normalized = "".join(character for character in normalized if not unicodedata.combining(character))
    return tuple(int(part) if part.isdigit() else part for part in re.split(r"(\d+)", normalized))


GROUP_SORT_FIELDS = {
    "title": lambda group: natural_sort_key(group.name),
    "total": lambda group: group.total,
    "episodes": lambda group: group.episodes,
    "bonus": lambda group: group.bonus,
    "cs": lambda group: group.cs,
    "sk": lambda group: group.sk,
    "missing": lambda group: group.missing,
    "unknown": lambda group: group.unknown,
    "matched": lambda group: group.matched,
    "translated": lambda group: group.translated,
}


def normalize_group_sort(sort: str | None, direction: str | None, query: str) -> tuple[str, str]:
    if sort not in GROUP_SORT_FIELDS:
        return ("relevance", "asc") if query else ("matched", "desc")
    return sort, direction if direction in {"asc", "desc"} else "asc"


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
    videos: Iterable[Video], filter_name: str, query: str | None = None,
    sort: str | None = None, direction: str | None = None,
) -> CatalogResults:
    query_text = normalize_search_query(query)
    folded_query = query_text.casefold()
    groups: dict[str, SeriesSummary] = {}
    all_by_title: dict[str, list[Video]] = {}
    for video in list(videos):
        catalog_title = video.catalog_title
        collection = catalog_title.collection if catalog_title else video.catalog_collection
        root_collection = meaningful_root_collection(video)
        if root_collection is not None:
            collection = root_collection
            if catalog_title and catalog_title.collection is not root_collection:
                catalog_title = None
        identity = determine_parent_series(video.relative_path)
        is_unassigned_root = is_root_video(video) and root_collection is None
        group_path = (
            ROOT_FOLDER if is_unassigned_root
            else collection.relative_root_path if collection else identity.relative_path
        )
        group_name = (
            ROOT_VIDEO_GROUP_LABEL if is_unassigned_root
            else catalog_collection_display_title(collection) if collection else identity.name
        )
        all_by_title.setdefault(group_path, []).append(video)
        summary = groups.setdefault(
            group_path,
            SeriesSummary(
                name=group_name, relative_path=group_path,
                catalog_title_id=video.catalog_title_id,
                catalog_collection_id=(collection.id if collection else None)
                if not is_unassigned_root else None,
                metadata_status=catalog_title.metadata_status if catalog_title else "unlinked",
                is_root_group=is_unassigned_root,
            ),
        )
        if catalog_title:
            summary.part_ids.add(catalog_title.id)
            if catalog_title.metadata_status in {"linked_auto", "linked_manual"}:
                summary.linked_part_ids.add(catalog_title.id)
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
        first_title = title_videos_list[0].catalog_title
        first_collection = (
            first_title.collection if first_title else title_videos_list[0].catalog_collection
        )
        root_collection = meaningful_root_collection(title_videos_list[0])
        if root_collection is not None:
            first_collection = root_collection
        identity = determine_parent_series(title_videos_list[0].relative_path)
        is_unassigned_root = is_root_video(title_videos_list[0]) and root_collection is None
        display_name = (
            ROOT_VIDEO_GROUP_LABEL if is_unassigned_root
            else catalog_collection_display_title(first_collection)
            if first_collection else identity.name
        )
        display_path = (
            ROOT_FOLDER if is_unassigned_root
            else first_collection.relative_root_path if first_collection else identity.relative_path
        )
        filtered = [video for video in title_videos_list if video_matches_filter(video, filter_name)]
        title_matches = bool(folded_query) and (
            _contains_query(display_name, folded_query)
            or _contains_query(display_path, folded_query)
            or any(
                _contains_query(video.catalog_title.local_title, folded_query)
                for video in title_videos_list if video.catalog_title
            )
        )
        matched_videos = filtered if not folded_query or title_matches else [
            video for video in filtered if video_matches_search(video, folded_query)
        ]
        if matched_videos:
            matches_by_title[title_path] = sorted(matched_videos, key=video_sort_key)
            groups[title_path].matched = len(matched_videos)

    normalized_sort, normalized_direction = normalize_group_sort(sort, direction, query_text)
    selected_groups = [groups[path] for path in matches_by_title]
    if normalized_sort == "relevance":
        def relevance(summary: SeriesSummary):
            name = summary.name.casefold()
            if name == folded_query:
                rank = 0
            elif name.startswith(folded_query):
                rank = 1
            elif folded_query in name:
                rank = 2
            else:
                rank = 3
            return rank, natural_sort_key(summary.name)
        ordered_groups = sorted(selected_groups, key=relevance)
    else:
        field = GROUP_SORT_FIELDS[normalized_sort]
        if normalized_sort == "title":
            ordered_groups = sorted(
                selected_groups, key=field, reverse=normalized_direction == "desc"
            )
        else:
            ordered_groups = sorted(selected_groups, key=lambda summary: natural_sort_key(summary.name))
            ordered_groups = sorted(
                ordered_groups, key=field, reverse=normalized_direction == "desc"
            )
    return CatalogResults(
        ordered_groups, matches_by_title, query_text, normalized_sort, normalized_direction
    )


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
TRAILING_EPISODE = re.compile(r"\s+-\s+0*(\d{1,3})(?:v\d+)?$", re.IGNORECASE)
BARE_EPISODE = re.compile(r"0*(\d{1,3})(?:v\d+)?", re.IGNORECASE)


def derive_episode_number(filename: str) -> int | None:
    stem = PurePosixPath(filename).stem
    if match := EXPLICIT_EPISODE.search(stem):
        return int(match.group(1))
    if match := TRAILING_EPISODE.search(stem):
        return int(match.group(1))
    if match := BARE_EPISODE.fullmatch(stem):
        return int(match.group(1))
    return None


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
        video.file_type != "episode",
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


VIDEO_SORT_FIELDS = {"season", "episode", "filename", "type", "resolution", "audio", "path"}


def normalize_video_sort(sort: str | None, direction: str | None) -> tuple[str, str]:
    if sort not in VIDEO_SORT_FIELDS:
        return "default", "asc"
    return sort, direction if direction in {"asc", "desc"} else "asc"


def sort_title_videos(
    videos: Iterable[Video], sort: str | None = None, direction: str | None = None
) -> tuple[list[Video], str, str]:
    normalized_sort, normalized_direction = normalize_video_sort(sort, direction)
    values = list(videos)
    if normalized_sort == "default":
        return sorted(values, key=video_sort_key), normalized_sort, normalized_direction

    def field(video: Video):
        season = derive_season_info(video.relative_path).label or ""
        episode = derive_episode_number(video.filename)
        fields = {
            "season": natural_sort_key(season),
            "episode": (episode is None, episode or 0),
            "filename": natural_sort_key(video.filename),
            "type": (TYPE_ORDER.get(video.file_type, 99), natural_sort_key(video.file_type)),
            "resolution": (video.width or 0) * (video.height or 0),
            "audio": natural_sort_key(" ".join(
                f"{track.language} {track.codec or ''}" for track in video.audio_tracks
            )),
            "path": natural_sort_key(video.relative_path),
        }
        return fields[normalized_sort]

    return (
        sorted(values, key=field, reverse=normalized_direction == "desc"),
        normalized_sort,
        normalized_direction,
    )


def set_manual_hardsub(
    video: Video, mode: str, *, verified_at: datetime | None = None
) -> None:
    values = {
        "unknown": (False, False),
        "none": (False, False),
        "cs": (True, False),
        "sk": (False, True),
        "both": (True, True),
    }
    if mode not in values:
        raise ValueError("Neplatná hodnota ručního hardsubu")
    video.manual_hardsub_cs, video.manual_hardsub_sk = values[mode]
    video.manual_hardsub_verified_at = (
        None if mode == "unknown" else (verified_at or datetime.now(timezone.utc))
    )
