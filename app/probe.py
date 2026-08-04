from __future__ import annotations

import json
import logging
from pathlib import Path
import subprocess

logger = logging.getLogger(__name__)


class ProbeError(RuntimeError):
    pass


def probe_video(path: Path) -> dict:
    command = [
        "ffprobe", "-v", "error", "-show_format", "-show_streams",
        "-of", "json", "--", str(path),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProbeError(str(exc)) from exc
    if result.returncode:
        raise ProbeError(result.stderr.strip() or f"ffprobe skončil s kódem {result.returncode}")
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
