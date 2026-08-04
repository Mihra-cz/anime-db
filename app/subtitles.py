from __future__ import annotations

import html
from pathlib import Path
import re

SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".vtt"}
CZ_WORDS = {"aby", "ale", "ano", "budu", "bych", "co", "český", "dobře", "jsem", "jsi", "když", "který", "může", "něco", "není", "protože", "přijde", "tady", "takže", "taky", "tenhle", "vám", "všechno", "žádný"}
SK_WORDS = {"aby", "ale", "áno", "budem", "by som", "čo", "dobre", "keď", "ktorý", "môže", "niečo", "nie je", "pretože", "príde", "slovenský", "takže", "tiež", "tento", "vám", "všetko", "žiadny"}


def subtitle_matches(video: Path, subtitle: Path) -> bool:
    if subtitle.suffix.lower() not in SUBTITLE_EXTENSIONS:
        return False
    video_stem = video.stem.casefold()
    subtitle_stem = subtitle.stem.casefold()
    return subtitle_stem == video_stem or subtitle_stem.startswith(video_stem + ".")


def strip_formatting(text: str) -> str:
    text = re.sub(r"(?m)^\s*(?:Dialogue|Comment):[^,]*,[^,]*,[^,]*,[^,]*,[^,]*,[^,]*,[^,]*,[^,]*,[^,]*,", "", text)
    text = re.sub(r"\{[^}]*\}|<[^>]*>", " ", text)
    text = re.sub(r"(?m)^\s*(?:WEBVTT|\d+|\d\d?:\d\d(?::\d\d)?[,.]\d+\s*-->)?.*$", lambda m: "" if "-->" in m.group(0) or m.group(0).strip().isdigit() or m.group(0).strip() == "WEBVTT" else m.group(0), text)
    return html.unescape(text).replace("\\N", " ").replace("\\n", " ")


def detect_language(text: str) -> str:
    clean = strip_formatting(text).casefold()
    words = set(re.findall(r"[a-záčďéěíĺľňóôŕřšťúůýž]+", clean))
    cz_score = sum(2 if word in {"jsem", "jsi", "když", "něco", "protože", "všechno"} else 1 for word in words & CZ_WORDS)
    sk_score = sum(2 if word in {"áno", "keď", "niečo", "pretože", "všetko", "žiadny"} else 1 for word in words & SK_WORDS)
    cz_score += sum(clean.count(char) for char in "ě ř ů".split()) * 2
    sk_score += sum(clean.count(char) for char in "ľ ĺ ô ŕ".casefold().split()) * 2
    if max(cz_score, sk_score) < 2 or cz_score == sk_score:
        return "unknown"
    return "cs" if cz_score > sk_score else "sk"


def read_and_detect(path: Path) -> str:
    for encoding in ("utf-8-sig", "cp1250", "latin-1"):
        try:
            return detect_language(path.read_text(encoding=encoding, errors="strict"))
        except UnicodeError:
            continue
        except OSError:
            return "unknown"
    return "unknown"
