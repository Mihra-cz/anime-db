from __future__ import annotations

from dataclasses import dataclass
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


def translation_status(video: Video) -> TranslationStatus:
    internal = {track.normalized_language for track in video.internal_subtitles}
    external = {track.normalized_language for track in video.external_subtitles}
    target = {"cs", "sk"}
    internal_target = bool(internal & target)
    external_target = bool(external & target)
    source = "both" if internal_target and external_target else "internal" if internal_target else "external" if external_target else None
    return TranslationStatus(
        has_cs="cs" in internal or "cs" in external,
        has_sk="sk" in internal or "sk" in external,
        has_cs_or_sk=bool((internal | external) & target),
        subtitle_source=source,
        has_unknown="unknown" in internal or "unknown" in external,
    )
