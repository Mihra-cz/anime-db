from __future__ import annotations

import json
import logging
from pathlib import Path
import subprocess

logger = logging.getLogger(__name__)


class ProbeError(RuntimeError):
    pass


class MediaInfoError(RuntimeError):
    pass


def _run_command(command: list[str], *, timeout_seconds: float, tool_name: str):
    try:
        result = subprocess.run(
            command, capture_output=True, text=True,
            timeout=max(0.1, float(timeout_seconds)), check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(f"{tool_name} překročil timeout {timeout_seconds:g} s") from exc
    except OSError as exc:
        raise ProbeError(f"{tool_name} nelze spustit: {exc}") from exc
    if result.returncode:
        raise ProbeError(
            result.stderr.strip() or f"{tool_name} skončil s kódem {result.returncode}"
        )
    return result


def probe_video(path: Path, timeout_seconds: float = 60) -> dict:
    command = [
        "ffprobe", "-v", "error", "-show_format", "-show_streams",
        "-of", "json", "--", str(path),
    ]
    result = _run_command(command, timeout_seconds=timeout_seconds, tool_name="ffprobe")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeError("ffprobe vrátil neplatný JSON") from exc

    streams = payload.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    duration_value = payload.get("format", {}).get("duration") or video.get("duration")
    return {
        "duration": float(duration_value) if duration_value not in (None, "N/A") else None,
        "video_codec": video.get("codec_name"),
        "width": video.get("width"),
        "height": video.get("height"),
        "audio": [
            {"stream_index": s.get("index", 0), "codec": s.get("codec_name"),
             "language": s.get("tags", {}).get("language", "unknown").lower()}
            for s in streams if s.get("codec_type") == "audio"
        ],
        "subtitles": [
            {"stream_index": s.get("index", 0), "codec": s.get("codec_name"),
             "language": s.get("tags", {}).get("language", "unknown").lower(),
             "title": s.get("tags", {}).get("title")}
            for s in streams if s.get("codec_type") == "subtitle"
        ],
    }


def probe_mediainfo(path: Path, timeout_seconds: float = 60) -> dict:
    """Optional safe wrapper; MediaInfo is not currently part of the scanner pipeline."""
    try:
        result = _run_command(
            ["mediainfo", "--Output=JSON", "--", str(path)],
            timeout_seconds=timeout_seconds, tool_name="MediaInfo",
        )
    except ProbeError as exc:
        raise MediaInfoError(str(exc)) from exc
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise MediaInfoError("MediaInfo vrátil neplatný JSON") from exc
