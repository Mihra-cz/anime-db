from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import PurePosixPath
import re
from typing import Literal
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
    supplementary = detect_episode_number(PurePosixPath(relative_path).name)
    if supplementary.is_supplementary:
        return {
            "ova": "ova", "special": "special", "ncop": "ncop", "nced": "nced",
            "preview": "pv",
        }.get(supplementary.supplementary_type or "", "other")
    if "ncop" in compact or "creditlessopening" in compact:
        return "ncop"
    if "nced" in compact or "creditlessending" in compact:
        return "nced"
    if "ova" in token_set or "oad" in token_set:
        return "ova"
    if "special" in token_set or "specials" in token_set or "sp" in token_set:
        return "special"
    if token_set & {"short", "shorts", "bonus", "bonuses", "extra", "extras"}:
        return "other"
    if "pv" in token_set or "preview" in token_set or "trailer" in token_set:
        return "pv"
    if "cm" in token_set or "commercial" in token_set:
        return "cm"
    if "menu" in token_set:
        return "menu"
    filename = value.rsplit("/", 1)[-1]
    detection = detect_episode_number(filename)
    if detection.is_nonstandard:
        return "other"
    if "episode" in token_set or detection.is_standard:
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


JapaneseAudioStatus = Literal["present", "missing", "unknown", "no_audio"]
SubtitleStatus = Literal["cs_sk_available", "en_only", "no_subtitles"]
CsSkSubtitlePriority = Literal["none", "normal", "high"]
SubtitleSource = Literal["internal", "external", "hardsub"]


@dataclass(frozen=True)
class AudioLanguageTrack:
    stream_index: int
    codec: str | None
    raw_language: str
    normalized_language: str


@dataclass(frozen=True)
class VideoLanguageProfile:
    audio_tracks: tuple[AudioLanguageTrack, ...]
    audio_languages: tuple[str, ...]
    japanese_audio_status: JapaneseAudioStatus
    has_japanese_audio: bool
    internal_subtitle_languages: frozenset[str]
    external_subtitle_languages: frozenset[str]
    hardsub_languages: frozenset[str]
    sources_by_language: Mapping[str, frozenset[SubtitleSource]]
    has_cs: bool
    has_sk: bool
    has_cs_or_sk: bool
    has_en: bool
    has_unknown_subtitle_language: bool
    subtitle_status: SubtitleStatus
    needs_cs_sk_subtitles: bool
    cs_sk_subtitle_priority: CsSkSubtitlePriority


@dataclass(frozen=True)
class _SubtitleLanguageProfile:
    internal_languages: frozenset[str]
    external_languages: frozenset[str]
    hardsub_languages: frozenset[str]
    sources_by_language: Mapping[str, frozenset[SubtitleSource]]
    has_cs: bool
    has_sk: bool
    has_cs_or_sk: bool
    has_en: bool
    has_unknown: bool
    status: SubtitleStatus
    needs_cs_sk: bool
    priority: CsSkSubtitlePriority


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


TITLE_NAME_PREFERENCES = ("romaji", "english", "native")
TITLE_NAME_PREFERENCE_LABELS = {
    "romaji": "Romaji",
    "english": "Anglický",
    "native": "Originální",
}


def normalize_title_name_preference(value: object, default: str = "romaji") -> str:
    normalized_default = str(default or "romaji").strip().casefold()
    if normalized_default not in TITLE_NAME_PREFERENCES:
        normalized_default = "romaji"
    normalized = str(value or "").strip().casefold()
    return normalized if normalized in TITLE_NAME_PREFERENCES else normalized_default


def filename_display_title(filename: str) -> str | None:
    """Odstraní pouze suffix epizody, který bezpečně rozpoznal episode parser."""
    detection = detect_episode_number(filename)
    if detection.kind == "unknown":
        return None
    stem = PurePosixPath(filename).stem.strip()
    match = (
        DISPLAY_TITLE_OVA_PART_SUFFIX.fullmatch(stem)
        or DISPLAY_TITLE_EPISODE_SUFFIX.fullmatch(stem)
    )
    if match is None:
        return None
    candidate = match.group("title").strip().rstrip("-_. ").strip()
    if candidate.casefold() in {"episode", "ep", "e"}:
        return None
    return candidate if len(candidate) >= 2 else None


def title_filename_display_title(videos: Iterable[Video]) -> str | None:
    """Vrátí shodný bezpečný prefix epizodních filename, jinak žádný odhad."""
    candidates = [
        candidate
        for video in videos
        if (candidate := filename_display_title(video.filename)) is not None
    ]
    if not candidates:
        return None
    normalized = {normalize_title(candidate) for candidate in candidates}
    return candidates[0] if len(normalized) == 1 else None


def _catalog_title_explicit_display_title(
    title: CatalogTitle, preference: object,
) -> str | None:
    """Vrátí ruční nebo metadata název bez lokálních fallbacků."""
    manual = (title.manual_display_title or "").strip()
    if manual:
        return manual
    metadata = title.metadata_record
    if metadata is not None:
        preferred = normalize_title_name_preference(preference)
        orders = {
            "english": ("title_english", "title_romaji", "title_native"),
            "romaji": ("title_romaji", "title_english", "title_native"),
            "native": ("title_native", "title_romaji", "title_english"),
        }
        for field in orders[preferred]:
            value = (getattr(metadata, field, None) or "").strip()
            if value:
                return value
        legacy_display = (metadata.display_title or "").strip()
        if legacy_display:
            return legacy_display
    return None


def catalog_title_display_title(
    title: CatalogTitle, preference: object = "romaji",
    *, videos: Iterable[Video] | None = None,
) -> str:
    """Vrátí prezentační název bez změny lokální identity nebo metadat."""
    explicit = _catalog_title_explicit_display_title(title, preference)
    if explicit:
        return explicit
    filename_title = title_filename_display_title(
        videos if videos is not None else title.videos
    )
    if filename_title:
        return filename_title
    local = (title.local_title or "").strip()
    return local or "Titul bez názvu"


COLLECTION_STRUCTURAL_PART_SUFFIX = re.compile(
    r"^(?P<base>.+?)(?:\s+[-–—:]\s*|\s+)part\s+(?P<number>[1-9]\d*)\s*$",
    re.IGNORECASE,
)


def _collection_structural_part_suffix(value: str) -> tuple[str, int] | None:
    match = COLLECTION_STRUCTURAL_PART_SUFFIX.fullmatch(value.strip())
    if match is None:
        return None
    base = match.group("base").strip()
    return (base, int(match.group("number"))) if base else None


def catalog_collection_display_title(
    collection: CatalogCollection, preference: object = "romaji",
    *, titles: Iterable[CatalogTitle] | None = None,
) -> str:
    """Vrátí hlavní název kolekce z ruční volby, metadat nebo lokální identity."""
    manual = (collection.manual_display_title or "").strip()
    if manual:
        return manual

    candidates: dict[tuple[str, int], CatalogTitle] = {}
    for title in collection.titles if titles is None else titles:
        belongs_to_collection = (
            title.collection is collection
            or collection.id is not None
            and title.catalog_collection_id == collection.id
        )
        if not belongs_to_collection:
            continue
        key = ("db", title.id) if title.id is not None else ("object", id(title))
        candidates[key] = title
    ordered = sorted(
        candidates.values(),
        key=lambda title: (
            title.effective_sort_order,
            title.id is None,
            title.id or 0,
            title.local_title.casefold(),
        ),
    )
    explicit_titles = [
        (title, explicit)
        for title in ordered
        if (explicit := _catalog_title_explicit_display_title(title, preference))
    ]
    sibling_part_numbers: dict[str, set[int]] = {}
    for _, explicit in explicit_titles:
        suffix = _collection_structural_part_suffix(explicit)
        if suffix is not None:
            base, number = suffix
            sibling_part_numbers.setdefault(
                " ".join(base.casefold().split()), set()
            ).add(number)

    for title, explicit in explicit_titles:
        suffix = _collection_structural_part_suffix(explicit)
        if suffix is not None:
            base, _ = suffix
            sibling_confirms_parts = len(sibling_part_numbers.get(
                " ".join(base.casefold().split()), set()
            )) >= 2
            if title.effective_part_type in {"part", "cour"} or sibling_confirms_parts:
                return base
        return explicit

    local = (collection.local_title or "").strip()
    return local or "Kolekce bez názvu"


def catalog_title_series_label(title: CatalogTitle) -> str:
    """Vrátí popisek části výhradně z hierarchie CatalogTitle."""
    part_type = title.effective_part_type
    if part_type == "part":
        # Part identity is composed from two independent numeric axes.  A
        # legacy season_label_manual such as "Part 2" must not masquerade as
        # the season scope.
        season_label = (
            f"S{title.effective_season_number}"
            if title.effective_season_number is not None else None
        )
        part_label = (
            f"Part {title.effective_part_number}"
            if title.effective_part_number is not None else None
        )
        return " · ".join(
            value for value in (season_label, part_label) if value
        ) or "—"
    season_label = title.effective_season_label or (
        f"S{title.effective_season_number}"
        if title.effective_season_number is not None else None
    )
    if part_type == "season":
        return season_label or "—"
    if part_type == "cour" and title.effective_part_number is not None:
        return f"Cour {title.effective_part_number}"
    if season_label:
        return season_label
    return {
        "film": "Film", "ova": "OVA", "special": "Special",
        "preview": "Preview", "recap": "Recap", "bonus": "Bonus",
        "other": "Other",
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


def _build_subtitle_language_profile(video: Video) -> _SubtitleLanguageProfile:
    internal_languages = frozenset(
        (track.normalized_language or "unknown").strip().casefold()
        for track in video.internal_subtitles
    )
    external_languages = frozenset(
        (track.normalized_language or "unknown").strip().casefold()
        for track in video.external_subtitles
    )

    # translation_status() historicky započítává uložené ruční příznaky i u
    # legacy řádku bez timestampu. Profil tento význam existujících dat nemění.
    hardsub_languages = frozenset(
        language
        for language, enabled in (
            ("cs", video.manual_hardsub_cs),
            ("sk", video.manual_hardsub_sk),
        )
        if enabled
    )

    mutable_sources: dict[str, set[SubtitleSource]] = {}
    for language in internal_languages:
        mutable_sources.setdefault(language, set()).add("internal")
    for language in external_languages:
        mutable_sources.setdefault(language, set()).add("external")
    for language in hardsub_languages:
        mutable_sources.setdefault(language, set()).add("hardsub")
    sources_by_language = {
        language: frozenset(sources)
        for language, sources in mutable_sources.items()
    }

    has_cs = "cs" in sources_by_language
    has_sk = "sk" in sources_by_language
    has_cs_or_sk = has_cs or has_sk
    # EN fallback je záměrně pouze interní subtitle stream.
    has_en = "eng" in internal_languages
    has_unknown = "unknown" in internal_languages or "unknown" in external_languages

    if has_cs_or_sk:
        status: SubtitleStatus = "cs_sk_available"
        priority: CsSkSubtitlePriority = "none"
    elif has_en:
        status = "en_only"
        priority = "normal"
    else:
        status = "no_subtitles"
        priority = "high"

    return _SubtitleLanguageProfile(
        internal_languages=internal_languages,
        external_languages=external_languages,
        hardsub_languages=hardsub_languages,
        sources_by_language=sources_by_language,
        has_cs=has_cs,
        has_sk=has_sk,
        has_cs_or_sk=has_cs_or_sk,
        has_en=has_en,
        has_unknown=has_unknown,
        status=status,
        needs_cs_sk=not has_cs_or_sk,
        priority=priority,
    )


def build_video_language_profile(video: Video) -> VideoLanguageProfile:
    """Odvodí jazykový stav videa bez zápisu nebo změny databázových entit."""
    audio_tracks = tuple(
        AudioLanguageTrack(
            stream_index=track.stream_index,
            codec=track.codec,
            raw_language=track.language or "unknown",
            normalized_language=normalize_language(track.language),
        )
        for track in sorted(video.audio_tracks, key=lambda item: item.stream_index)
    )
    audio_languages = tuple(dict.fromkeys(
        track.normalized_language for track in audio_tracks
    ))
    if "jpn" in audio_languages:
        japanese_audio_status: JapaneseAudioStatus = "present"
    elif not audio_tracks:
        japanese_audio_status = "no_audio"
    elif "unknown" in audio_languages:
        japanese_audio_status = "unknown"
    else:
        japanese_audio_status = "missing"

    subtitle = _build_subtitle_language_profile(video)

    return VideoLanguageProfile(
        audio_tracks=audio_tracks,
        audio_languages=audio_languages,
        japanese_audio_status=japanese_audio_status,
        has_japanese_audio=japanese_audio_status == "present",
        internal_subtitle_languages=subtitle.internal_languages,
        external_subtitle_languages=subtitle.external_languages,
        hardsub_languages=subtitle.hardsub_languages,
        sources_by_language=subtitle.sources_by_language,
        has_cs=subtitle.has_cs,
        has_sk=subtitle.has_sk,
        has_cs_or_sk=subtitle.has_cs_or_sk,
        has_en=subtitle.has_en,
        has_unknown_subtitle_language=subtitle.has_unknown,
        subtitle_status=subtitle.status,
        needs_cs_sk_subtitles=subtitle.needs_cs_sk,
        cs_sk_subtitle_priority=subtitle.priority,
    )


def translation_status(video: Video) -> TranslationStatus:
    profile = _build_subtitle_language_profile(video)
    internal = profile.internal_languages
    external = profile.external_languages
    target = {"cs", "sk"}
    internal_target = bool(internal & target)
    external_target = bool(external & target)
    source = "both" if internal_target and external_target else "internal" if internal_target else "external" if external_target else None
    automatic_has_cs = "cs" in internal or "cs" in external
    automatic_has_sk = "sk" in internal or "sk" in external
    return TranslationStatus(
        has_cs=profile.has_cs,
        has_sk=profile.has_sk,
        has_cs_or_sk=profile.has_cs_or_sk,
        subtitle_source=source,
        has_unknown=profile.has_unknown,
        automatic_has_cs=automatic_has_cs,
        automatic_has_sk=automatic_has_sk,
    )


GENERIC_ROOTS = {"anime", "library", "media", "videos", "video"}
STRUCTURAL_DIRECTORY = re.compile(
    r"^(?:(?:s[ée]rie|series|season)\s*[-_. ]*\d+|s\s*[-_. ]*\d+)"
    r"(?:\s+(?:shorts?|specials?|sps?|ova|oad|extras?|bonus(?:es)?|nc|ncop|nced|"
    r"op|ed|previews?|recaps?|movies?|films?|pv|cm\s*[&+]\s*pv))?"
    r"(?:\s*(?:\([^)]*\)|[A-Z]\d{2}(?:-[A-Z]\d{2})?))?$"
    r"|^(?:(?:cour|part)\s*[-_. ]*\d+)"
    r"(?:\s*(?:\([^)]*\)|[A-Z]\d{2}(?:-[A-Z]\d{2})?))?$"
    r"|^(?:shorts?|specials?|sps?|extras?|bonus(?:es)?|ova|oad|nc|ncop|nced|op|ed|"
    r"previews?|recaps?|movies?|films?|pv|cm\s*[&+]\s*pv)"
    r"(?:\s*(?:\([^)]*\)|[A-Z]\d{2}(?:-[A-Z]\d{2})?))?$",
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
    "films": "Filmy",
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
    "all-duplicates": "Všechny duplicity",
    "manual-duplicate-suspected": "Ruční podezření na duplicitu",
}


def unresolved_duplicate_video_ids(videos: Iterable[Video]) -> set[int]:
    """Return current unresolved duplicate members using the numbering workflow."""
    # Local import avoids the catalog <-> numbering module cycle. The actual
    # duplicate rules deliberately remain owned by unresolved_duplicate_groups.
    from .numbering import unresolved_duplicate_groups

    videos_by_title: dict[int, list[Video]] = {}
    for video in videos:
        if video.catalog_title_id is not None:
            videos_by_title.setdefault(video.catalog_title_id, []).append(video)
    return {
        video.id
        for title_videos_list in videos_by_title.values()
        for group in unresolved_duplicate_groups(title_videos_list)
        for video in group.videos
        if video.id is not None
    }


def is_film_video(video: Video) -> bool:
    """Use the authoritative title hierarchy shared by statistics and filters."""
    return bool(
        video.catalog_title is not None
        and video.catalog_title.effective_part_type == "film"
    )


def video_matches_filter(
    video: Video, filter_name: str, *,
    unresolved_duplicate_ids: set[int] | None = None,
) -> bool:
    status = translation_status(video)
    predicates = {
        "all": True,
        "only-cs": status.has_cs and not status.has_sk,
        "only-sk": status.has_sk and not status.has_cs,
        "both": status.has_cs and status.has_sk,
        "missing": not status.has_cs_or_sk,
        "unknown": status.has_unknown,
        "episodes": video.file_type == "episode",
        "films": is_film_video(video),
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
        "all-duplicates": bool(
            video.duplicate_of_video_id is not None
            or video.id is not None
            and video.id in (unresolved_duplicate_ids or set())
        ),
        "manual-duplicate-suspected": video.duplicate_status_manual == "suspected",
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
    title_name_preference: object = "romaji",
) -> CatalogResults:
    video_list = list(videos)
    unresolved_duplicate_ids = (
        unresolved_duplicate_video_ids(video_list)
        if filter_name == "all-duplicates" else None
    )
    query_text = normalize_search_query(query)
    folded_query = query_text.casefold()
    groups: dict[str, SeriesSummary] = {}
    all_by_title: dict[str, list[Video]] = {}
    for video in video_list:
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
            else catalog_collection_display_title(collection, titles=())
            if collection else identity.name
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
        filter_match = video_matches_filter(
            video, filter_name,
            unresolved_duplicate_ids=unresolved_duplicate_ids,
        )
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
            else catalog_collection_display_title(
                first_collection,
                title_name_preference,
                titles=(
                    video.catalog_title
                    for video in title_videos_list
                    if video.catalog_title is not None
                ),
            )
            if first_collection else identity.name
        )
        groups[title_path].name = display_name
        display_path = (
            ROOT_FOLDER if is_unassigned_root
            else first_collection.relative_root_path if first_collection else identity.relative_path
        )
        filtered = [
            video for video in title_videos_list
            if video_matches_filter(
                video, filter_name,
                unresolved_duplicate_ids=unresolved_duplicate_ids,
            )
        ]
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
SXXEXX_TOKEN = re.compile(
    r"(?<![a-z0-9])s0*(?P<season>\d{1,3})[\s._-]*"
    r"e0*(?P<episode>\d{1,3})(?:v\d+)?(?=$|[^a-z0-9])",
    re.IGNORECASE,
)
SXXEXX_BRACKETED_SUPPLEMENTARY = re.compile(
    r"(?<![a-z0-9])s0*(?P<season>\d{1,3})[\s._-]*"
    r"e0*(?P<episode>\d{1,3})(?:v\d+)?[\s._-]*"
    r"\[\s*(?P<type>SP)\s*\](?P<title>.*)$",
    re.IGNORECASE,
)
TRAILING_EPISODE = re.compile(r"\s+-\s+0*(\d{1,3})(?:v\d+)?$", re.IGNORECASE)
TRAILING_PLAIN_EPISODE = re.compile(r"\s+0*(\d{1,3})(?:v\d+)?$", re.IGNORECASE)
SUPPLEMENTARY_SEQUENCE = re.compile(
    r"(?:^|[^a-z0-9])"
    r"(?P<type>NCOP|NCED|OVA|OAD|SPECIALS?|OP|ED|PREVIEWS?|PV|RECAPS?|"
    r"BONUS(?:ES)?|EXTRAS?)"
    r"\s*(?:(?:P|EPISODE|EP|E)\s*)?0*(?P<number>\d{1,3})(?:v\d+)?$",
    re.IGNORECASE,
)
BARE_EPISODE = re.compile(r"0\d{1,2}(?:v\d+)?", re.IGNORECASE)
EXPLICIT_FRACTIONAL_EPISODE = re.compile(
    r"(?:^|[^a-z0-9])(?:episode|ep|e)\s*[-_. ]*0*(\d{1,3})\.(\d+)"
    r"(?:v\d+)?(?:[^a-z0-9]|$)",
    re.IGNORECASE,
)
TRAILING_FRACTIONAL_EPISODE = re.compile(
    r"(?:\s+-\s+|\s+)0*(\d{1,3})\.(\d+)(?:v\d+)?$", re.IGNORECASE
)
DISPLAY_TITLE_EPISODE_SUFFIX = re.compile(
    r"(?P<title>.+?)(?:\s+-\s+|\s+)"
    r"(?:(?:episode|ep|e)\s*[-_. ]*)?"
    r"(?:0*\d{1,3}(?:\.\d+)?)(?:v\d+)?$",
    re.IGNORECASE,
)
DISPLAY_TITLE_OVA_PART_SUFFIX = re.compile(
    r"(?P<title>.+?)(?:\s+-\s+|\s+)OVA\s+P0*\d{1,3}(?:v\d+)?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class EpisodeNumberDetection:
    kind: str
    number: int | None = None
    fraction: str | None = None
    supplementary_type: str | None = None
    context_hint: str | None = None
    season_hint: int | None = None
    filename_episode_hint: int | None = None
    title_candidate: str | None = None

    @property
    def is_standard(self) -> bool:
        return self.kind == "standard"

    @property
    def is_nonstandard(self) -> bool:
        return self.kind in {"zero", "fractional"}

    @property
    def is_supplementary(self) -> bool:
        return self.kind == "supplementary"

    @property
    def supplementary_number(self) -> int | None:
        return self.number if self.is_supplementary else None

    @property
    def display_value(self) -> str | None:
        if self.kind == "zero":
            return "00"
        if self.kind == "fractional" and self.number is not None and self.fraction:
            return f"{self.number}.{self.fraction}"
        if self.kind == "standard" and self.number is not None:
            return str(self.number)
        if self.is_supplementary and self.number is not None:
            label = {
                "ova": "OVA", "special": "Special", "ncop": "NCOP", "nced": "NCED",
                "op": "OP", "ed": "ED", "preview": "Preview", "recap": "Recap",
                "bonus": "Bonus",
            }.get(self.supplementary_type or "", "Doplněk")
            return f"{label} {self.number:02d}"
        return None


def detect_episode_number(filename: str) -> EpisodeNumberDetection:
    """Bezpečně rozliší standardní, nulové, desetinné a neznámé číslování."""
    stem = PurePosixPath(filename).stem
    if match := SXXEXX_BRACKETED_SUPPLEMENTARY.search(stem):
        title_candidate = match.group("title").lstrip(" -_.").strip() or None
        return EpisodeNumberDetection(
            "supplementary",
            supplementary_type="special",
            context_hint=stem[:match.start()].rstrip(" -_.") or None,
            season_hint=int(match.group("season")),
            filename_episode_hint=int(match.group("episode")),
            title_candidate=title_candidate,
        )
    if match := SUPPLEMENTARY_SEQUENCE.search(stem):
        number = int(match.group("number"))
        raw_type = match.group("type").casefold()
        supplementary_type = {
            "oad": "ova", "specials": "special", "previews": "preview", "pv": "preview",
            "recaps": "recap", "bonuses": "bonus", "extra": "bonus", "extras": "bonus",
        }.get(raw_type, raw_type)
        context_hint = stem[:match.start()].rstrip(" -_.") or None
        return EpisodeNumberDetection(
            "supplementary", number, supplementary_type=supplementary_type,
            context_hint=context_hint,
        )
    if match := SXXEXX_TOKEN.search(stem):
        number = int(match.group("episode"))
        title_candidate = stem[match.end():].lstrip(" -_.").strip() or None
        return EpisodeNumberDetection(
            "zero" if number == 0 else "standard",
            number,
            season_hint=int(match.group("season")),
            filename_episode_hint=number,
            title_candidate=title_candidate,
        )
    for pattern in (EXPLICIT_FRACTIONAL_EPISODE, TRAILING_FRACTIONAL_EPISODE):
        if match := pattern.search(stem):
            return EpisodeNumberDetection(
                "fractional", int(match.group(1)), match.group(2)
            )
    for pattern in (EXPLICIT_EPISODE, TRAILING_EPISODE, TRAILING_PLAIN_EPISODE):
        if match := pattern.search(stem):
            number = int(match.group(1))
            return EpisodeNumberDetection("zero" if number == 0 else "standard", number)
    if BARE_EPISODE.fullmatch(stem):
        number = int(re.match(r"0*(\d+)", stem, re.IGNORECASE).group(1))
        return EpisodeNumberDetection("zero" if number == 0 else "standard", number)
    return EpisodeNumberDetection("unknown")


def derive_episode_number(filename: str) -> int | None:
    detection = detect_episode_number(filename)
    return detection.number if detection.is_standard else None


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
