from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.catalog import classify_video, normalize_language, normalize_title
from app.hierarchy import derive_library_hierarchy
from app.hierarchy_review import (
    collection_requires_review, definition_from_title, extract_local_period_hint,
    manual_split_titles, preview_assignments,
)
from app.models import (
    AudioTrack, CatalogCollection, CatalogTitle, ExternalSubtitle, InternalSubtitle, Video,
)
from app.numbering import recalculate_collection_numbering
from app.probe import ProbeError, probe_video
from app.subtitles import SUBTITLE_EXTENSIONS, read_and_detect, subtitle_matches

logger = logging.getLogger(__name__)
VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi"}
IGNORED_DIRECTORIES = {"#recycle", "@eadir"}
MAX_REMOVAL_PERCENT = 20


class LibrarySafetyError(RuntimeError):
    def __init__(self, message: str, *, confirmation_allowed: bool = False):
        super().__init__(message)
        self.confirmation_allowed = confirmation_allowed


@dataclass
class ScanResult:
    found: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    removed: int = 0
    errors: int = 0


def iter_videos(root: Path):
    def raise_walk_error(error: OSError) -> None:
        raise error

    for current, directories, files in os.walk(
        root, topdown=True, onerror=raise_walk_error, followlinks=False
    ):
        directories[:] = sorted(
            d for d in directories
            if d.casefold() not in IGNORED_DIRECTORIES and not d.startswith(".")
        )
        for filename in sorted(files):
            path = Path(current) / filename
            if path.suffix.lower() in VIDEO_EXTENSIONS:
                yield path


def is_on_mounted_source(root: Path) -> bool:
    """Return true for a path at or below a mount other than the root filesystem."""
    current = root.resolve()
    while current != current.parent:
        if os.path.ismount(current):
            return True
        current = current.parent
    return False


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


def _scan_library(
    session: Session,
    root: Path,
    *,
    require_mount: bool = False,
    confirm_deletions: bool = False,
) -> ScanResult:
    root = root.resolve()
    if not root.is_dir():
        raise LibrarySafetyError(
            f"ANIME_PATH není dostupný adresář: {root}. Knihovna může být odpojená."
        )
    if require_mount and not is_on_mounted_source(root):
        raise LibrarySafetyError(
            f"ANIME_PATH {root} neleží na připojeném zdroji. Knihovna může být odpojená."
        )

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

    missing = [(key, video) for key, video in existing.items() if key not in seen]
    existing_count = len(existing)
    if existing_count and result.found == 0:
        raise LibrarySafetyError(
            f"Sken nenašel žádná videa, ale databáze jich obsahuje {existing_count}. "
            "Knihovna může být odpojená; databáze nebyla změněna."
        )
    if (
        existing_count
        and len(missing) * 100 > existing_count * MAX_REMOVAL_PERCENT
        and not confirm_deletions
    ):
        percent = len(missing) * 100 / existing_count
        raise LibrarySafetyError(
            f"Sken by odstranil {len(missing)} z {existing_count} videí ({percent:.1f} %). "
            "Knihovna může být odpojená. Zkontrolujte ji a případné odstranění explicitně potvrďte.",
            confirmation_allowed=True,
        )

    # Mazání je záměrně až poslední operace po dokončení průchodu a bezpečnostních kontrolách.
    for _, video in missing:
        session.delete(video)
        result.removed += 1
    session.flush()
    current_videos = [
        video for video in session.scalars(select(Video)).all()
        if video.relative_path in seen
    ]
    hierarchy = derive_library_hierarchy([video.relative_path for video in current_videos])
    collections = {
        value.relative_root_path: value
        for value in session.scalars(select(CatalogCollection)).all()
    }
    titles = {
        value.relative_root_path: value
        for value in session.scalars(select(CatalogTitle)).all()
    }
    videos_by_collection_path: dict[str, list[Video]] = {}
    for video in current_videos:
        identity = hierarchy[video.relative_path]
        videos_by_collection_path.setdefault(
            identity.collection.relative_root_path, []
        ).append(video)
    for video in current_videos:
        identity = hierarchy[video.relative_path]
        collection_data, title_data = identity.collection, identity.title
        collection = collections.get(collection_data.relative_root_path)
        if collection is None:
            collection = CatalogCollection(
                local_title=collection_data.local_title,
                normalized_local_title=normalize_title(collection_data.local_title),
                relative_root_path=collection_data.relative_root_path,
            )
            session.add(collection)
            session.flush()
            collections[collection.relative_root_path] = collection
        video.catalog_collection = collection
        split_titles = manual_split_titles(collection)
        if split_titles:
            continue
        catalog_title = titles.get(title_data.relative_root_path)
        if catalog_title is None:
            catalog_title = CatalogTitle(
                local_title=title_data.local_title,
                normalized_local_title=normalize_title(title_data.local_title),
                relative_root_path=title_data.relative_root_path,
            )
            session.add(catalog_title)
            titles[catalog_title.relative_root_path] = catalog_title
        catalog_title.collection = collection
        if not catalog_title.hierarchy_manual_override:
            catalog_title.part_type = title_data.part_type
            catalog_title.season_number = title_data.season_number
            catalog_title.season_label = title_data.season_label
            catalog_title.original_folder_name = title_data.original_folder_name
            catalog_title.sort_order = title_data.sort_order
            catalog_title.part_number = (
                title_data.season_number
                if title_data.part_type in {"part", "cour"} else None
            )
        video.catalog_title = catalog_title
    for path, collection_videos in videos_by_collection_path.items():
        collection = collections[path]
        collection.local_period_hint = extract_local_period_hint(collection.local_title)
        split_titles = sorted(
            manual_split_titles(collection), key=lambda title: title.effective_sort_order
        )
        if split_titles:
            preview = preview_assignments(
                collection_videos, [definition_from_title(title) for title in split_titles]
            )
            for video in collection_videos:
                target = preview.assignments.get(video.id)
                video.catalog_title = split_titles[target] if target is not None else None
            if preview.conflicts:
                collection.hierarchy_status = "conflict"
                collection.hierarchy_note = "Video odpovídá více ručním částem."
                collection.hierarchy_verified_at = None
            elif preview.unmatched_video_ids:
                collection.hierarchy_status = "review_required"
                collection.hierarchy_note = "Nové nezařazené video."
                collection.hierarchy_verified_at = None
            continue
        reason = collection_requires_review(collection, collection_videos)
        if collection.hierarchy_status != "verified":
            collection.hierarchy_status = "review_required" if reason else "automatic"
            collection.hierarchy_note = reason
    session.flush()
    videos_by_title: dict[int, list[Video]] = {}
    for video in current_videos:
        videos_by_title.setdefault(video.catalog_title_id, []).append(video)
    session.expire_all()
    for collection in session.scalars(select(CatalogCollection).options(
        selectinload(CatalogCollection.titles).selectinload(CatalogTitle.metadata_record)
    )).all():
        recalculate_collection_numbering(collection, videos_by_title)
    session.commit()
    logger.info("Sken dokončen: found=%d created=%d updated=%d unchanged=%d removed=%d errors=%d",
                result.found, result.created, result.updated, result.unchanged, result.removed, result.errors)
    return result


def scan_library(
    session: Session,
    root: Path,
    *,
    require_mount: bool = False,
    confirm_deletions: bool = False,
) -> ScanResult:
    try:
        return _scan_library(
            session,
            root,
            require_mount=require_mount,
            confirm_deletions=confirm_deletions,
        )
    except Exception:
        session.rollback()
        logger.exception("Sken byl vrácen zpět kvůli neočekávané chybě")
        raise
