from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.catalog import classify_video, normalize_language
from app.models import AudioTrack, ExternalSubtitle, InternalSubtitle, Video
from app.probe import ProbeError, probe_video
from app.subtitles import SUBTITLE_EXTENSIONS, read_and_detect, subtitle_matches

logger = logging.getLogger(__name__)
VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi"}
IGNORED_DIRECTORIES = {"#recycle", "@eadir"}


@dataclass
class ScanResult:
    found: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    removed: int = 0
    errors: int = 0


def iter_videos(root: Path):
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        directories[:] = sorted(
            d for d in directories
            if d.casefold() not in IGNORED_DIRECTORIES and not d.startswith(".")
        )
        for filename in sorted(files):
            path = Path(current) / filename
            if path.suffix.lower() in VIDEO_EXTENSIONS:
                yield path


def _root_folder(relative: Path) -> str:
    return relative.parts[0] if len(relative.parts) > 1 else "."


def _external_subtitles(video_path: Path, library_root: Path) -> list[dict[str, str]]:
    try:
        candidates = video_path.parent.iterdir()
    except OSError:
        return []
    result = []
    for path in candidates:
        if path.is_file() and path.suffix.lower() in SUBTITLE_EXTENSIONS and subtitle_matches(video_path, path):
            language = read_and_detect(path)
            result.append({
                "relative_path": path.relative_to(library_root).as_posix(),
                "codec": path.suffix.lower().lstrip("."),
                "language": language,
                "normalized_language": normalize_language(language),
            })
    return result


def _sync_external_subtitles(
    session: Session, video: Video, subtitle_data: list[dict[str, str]]
) -> None:
    """Synchronize the relationship without replacing rows sharing a unique key."""
    with session.no_autoflush:
        existing = {subtitle.relative_path: subtitle for subtitle in video.external_subtitles}
        incoming = {subtitle["relative_path"]: subtitle for subtitle in subtitle_data}

        for relative_path, data in incoming.items():
            subtitle = existing.get(relative_path)
            if subtitle is None:
                video.external_subtitles.append(ExternalSubtitle(**data))
            else:
                subtitle.codec = data["codec"]
                subtitle.language = data["language"]
                subtitle.normalized_language = data["normalized_language"]

        for relative_path, subtitle in existing.items():
            if relative_path not in incoming:
                video.external_subtitles.remove(subtitle)


def _scan_library(session: Session, root: Path) -> ScanResult:
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"ANIME_PATH není dostupný adresář: {root}")

    result = ScanResult()
    existing = {v.relative_path: v for v in session.scalars(select(Video)).all()}
    seen: set[str] = set()
    for path in iter_videos(root):
        result.found += 1
        relative = path.relative_to(root)
        key = relative.as_posix()
        seen.add(key)
        video = existing.get(key)
        try:
            stat = path.stat()
            changed = video is None or video.size != stat.st_size or video.mtime_ns != stat.st_mtime_ns
            if video is None:
                video = Video(relative_path=key, root_folder=_root_folder(relative), filename=path.name,
                              size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                              file_type=classify_video(key))
                session.add(video)
                result.created += 1
            elif changed:
                result.updated += 1
            else:
                result.unchanged += 1

            if changed:
                metadata = probe_video(path)
                video.filename, video.root_folder = path.name, _root_folder(relative)
                video.file_type = classify_video(key)
                video.size, video.mtime_ns = stat.st_size, stat.st_mtime_ns
                video.duration, video.video_codec = metadata["duration"], metadata["video_codec"]
                video.width, video.height = metadata["width"], metadata["height"]
                video.audio_tracks = [AudioTrack(**track) for track in metadata["audio"]]
                video.internal_subtitles = [
                    InternalSubtitle(
                        **track,
                        normalized_language=normalize_language(track.get("language"), track.get("title")),
                    )
                    for track in metadata["subtitles"]
                ]
            _sync_external_subtitles(session, video, _external_subtitles(path, root))
        except (OSError, ProbeError, ValueError) as exc:
            result.errors += 1
            logger.warning("Nelze zpracovat %s: %s", key, exc)
            if key not in existing and video is not None:
                session.expunge(video)

    for key, video in existing.items():
        if key not in seen:
            session.delete(video)
            result.removed += 1
    session.commit()
    logger.info("Sken dokončen: found=%d created=%d updated=%d unchanged=%d removed=%d errors=%d",
                result.found, result.created, result.updated, result.unchanged, result.removed, result.errors)
    return result


def scan_library(session: Session, root: Path) -> ScanResult:
    try:
        return _scan_library(session, root)
    except Exception:
        session.rollback()
        logger.exception("Sken byl vrácen zpět kvůli neočekávané chybě")
        raise
