from __future__ import annotations

from dataclasses import dataclass
import logging
import multiprocessing
import os
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.catalog import (
    classify_video, is_root_video, meaningful_root_collection, normalize_language, normalize_title,
)
from app.hierarchy import derive_library_hierarchy
from app.hierarchy_evaluation import finalize_collection_hierarchy
from app.hierarchy_review import (
    definition_from_title, extract_local_period_hint,
    manual_split_titles, preview_assignments,
)
from app.models import (
    AudioTrack, CatalogCollection, CatalogTitle, ExternalSubtitle, InternalSubtitle, Video,
)
from app.numbering import recalculate_collection_numbering
from app.probe import ProbeError, probe_video
from app.subtitles import SUBTITLE_EXTENSIONS, read_and_detect, subtitle_matches

logger = logging.getLogger(__name__)
VIDEO_EXTENSIONS = {".mkv", ".mp4", ".m4v", ".avi"}
IGNORED_DIRECTORIES = {"#recycle", "@eadir"}
MAX_REMOVAL_PERCENT = 20


class LibrarySafetyError(RuntimeError):
    def __init__(self, message: str, *, confirmation_allowed: bool = False):
        super().__init__(message)
        self.confirmation_allowed = confirmation_allowed


class LibraryUnavailableError(LibrarySafetyError):
    def __init__(self, message: str, *, last_successful_file: str | None = None, result=None):
        super().__init__(message)
        self.last_successful_file = last_successful_file
        self.result = result


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


def _library_healthcheck_worker(root: str, require_mount: bool, sender) -> None:
    try:
        path = Path(root)
        if not path.is_dir():
            raise OSError("kořen knihovny není dostupný adresář")
        if require_mount and not is_on_mounted_source(path):
            raise OSError("kořen knihovny už neleží na připojeném zdroji")
        with os.scandir(path) as entries:
            next(entries, None)
        sender.send((True, None))
    except BaseException as exc:
        try:
            sender.send((False, str(exc)))
        except (BrokenPipeError, OSError):
            pass
    finally:
        sender.close()


def check_library_access(root: Path, *, timeout_seconds: float = 10, require_mount: bool = False) -> None:
    receiver, sender = multiprocessing.Pipe(duplex=False)
    process = multiprocessing.Process(
        target=_library_healthcheck_worker,
        args=(str(root), require_mount, sender),
        daemon=True,
    )
    process.start()
    sender.close()
    process.join(max(0.1, float(timeout_seconds)))
    if process.is_alive():
        process.terminate()
        process.join(1)
        if process.is_alive():
            process.kill()
            process.join(1)
        receiver.close()
        raise LibraryUnavailableError(
            f"Kontrola přístupu ke knihovně překročila timeout {timeout_seconds:g} s."
        )
    try:
        try:
            ok, detail = receiver.recv() if receiver.poll() else (False, "kontrola přístupu selhala")
        except EOFError:
            ok, detail = False, "proces kontroly přístupu skončil bez výsledku"
    finally:
        receiver.close()
    if not ok:
        raise LibraryUnavailableError(f"Knihovna není dostupná: {detail}.")


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
    ffprobe_timeout_seconds: float = 60,
    library_access_timeout_seconds: float = 10,
    library_healthcheck_interval_files: int = 25,
) -> ScanResult:
    root = root.absolute()
    result = ScanResult()
    last_successful_file: str | None = None
    try:
        check_library_access(
            root, timeout_seconds=library_access_timeout_seconds, require_mount=require_mount
        )
    except LibraryUnavailableError as exc:
        exc.result = result
        raise
    existing = {v.relative_path: v for v in session.scalars(select(Video)).all()}
    seen: set[str] = set()
    for path in iter_videos(root):
        if result.found and result.found % max(1, library_healthcheck_interval_files) == 0:
            try:
                check_library_access(
                    root, timeout_seconds=library_access_timeout_seconds,
                    require_mount=require_mount,
                )
            except LibraryUnavailableError as exc:
                logger.error(
                    "Knihovna přestala být dostupná během skenu; poslední úspěšný soubor=%s",
                    last_successful_file or "žádný",
                )
                raise LibraryUnavailableError(
                    f"Knihovna přestala být dostupná během skenu: {exc}",
                    last_successful_file=last_successful_file, result=result,
                ) from exc
        result.found += 1
        relative = path.relative_to(root)
        key = relative.as_posix()
        seen.add(key)
        video = existing.get(key)
        try:
            stat = path.stat()
            is_new = video is None
            changed = is_new or video.size != stat.st_size or video.mtime_ns != stat.st_mtime_ns
            if is_new:
                video = Video(relative_path=key, root_folder=_root_folder(relative), filename=path.name,
                              size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                              file_type=classify_video(key))
                session.add(video)
            if not changed:
                result.unchanged += 1

            if changed:
                metadata = probe_video(path, timeout_seconds=ffprobe_timeout_seconds)
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
                if is_new:
                    result.created += 1
                else:
                    result.updated += 1
            _sync_external_subtitles(session, video, _external_subtitles(path, root))
            last_successful_file = key
        except (OSError, ProbeError, ValueError) as exc:
            result.errors += 1
            logger.warning("Nelze zpracovat %s: %s", key, exc)
            if key not in existing and video is not None:
                session.expunge(video)

    try:
        check_library_access(
            root, timeout_seconds=library_access_timeout_seconds, require_mount=require_mount
        )
    except LibraryUnavailableError as exc:
        logger.error(
            "Knihovna přestala být dostupná po průchodu; poslední úspěšný soubor=%s",
            last_successful_file or "žádný",
        )
        raise LibraryUnavailableError(
            f"Knihovna přestala být dostupná během skenu před dokončením: {exc}",
            last_successful_file=last_successful_file, result=result,
        ) from exc

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
        for duplicate_copy in list(video.duplicate_copies):
            if duplicate_copy.relative_path in seen:
                duplicate_copy.duplicate_of = None
                duplicate_copy.duplicate_primary_missing = True
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
    for video in current_videos:
        if is_root_video(video):
            meaningful_collection = meaningful_root_collection(video)
            if meaningful_collection is None:
                video.catalog_title = None
                video.catalog_collection = None
            else:
                if (
                    video.catalog_title
                    and video.catalog_title.collection is not meaningful_collection
                ):
                    video.catalog_title = None
                video.catalog_collection = meaningful_collection
            continue
        identity = hierarchy[video.relative_path]
        collection_data, title_data = identity.collection, identity.title
        assigned_manual_title = (
            video.catalog_title
            if video.catalog_title is not None
            and video.catalog_title.hierarchy_manual_override
            and video.catalog_title.collection is not None
            else None
        )
        path_title = titles.get(title_data.relative_root_path)
        reassigned_path_title = (
            path_title
            if path_title is not None
            and path_title.hierarchy_manual_override
            and path_title.collection is not None
            and path_title.collection.relative_root_path
            != collection_data.relative_root_path
            else None
        )
        existing_title = assigned_manual_title or reassigned_path_title
        if existing_title is not None:
            video.catalog_title = existing_title
            video.catalog_collection = existing_title.collection
            continue
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
            catalog_title.part_number = title_data.part_number
            catalog_title.season_label = title_data.season_label
            catalog_title.original_folder_name = title_data.original_folder_name
            catalog_title.sort_order = title_data.sort_order
        video.catalog_title = catalog_title
    session.flush()
    videos_by_collection_path: dict[str, list[Video]] = {}
    for video in current_videos:
        if not is_root_video(video) and video.catalog_collection is not None:
            videos_by_collection_path.setdefault(
                video.catalog_collection.relative_root_path, []
            ).append(video)
    manual_split_status_locked: set[int] = set()
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
                if target is not None:
                    video.catalog_title = split_titles[target]
                elif (
                    video.catalog_title is None
                    or video.catalog_title.catalog_collection_id != collection.id
                ):
                    video.catalog_title = None
            unresolved_ids = tuple(
                video.id for video in collection_videos
                if video.id in preview.unmatched_video_ids and video.catalog_title is None
            )
            if preview.conflicts:
                collection.hierarchy_status = "conflict"
                collection.hierarchy_note = "Video odpovídá více ručním částem."
                collection.hierarchy_verified_at = None
                manual_split_status_locked.add(collection.id)
            elif unresolved_ids:
                collection.hierarchy_status = "review_required"
                collection.hierarchy_note = "Nové nezařazené video."
                collection.hierarchy_verified_at = None
                manual_split_status_locked.add(collection.id)
            continue
    session.flush()
    videos_by_title: dict[int, list[Video]] = {}
    videos_by_collection_id: dict[int, list[Video]] = {}
    for video in current_videos:
        if video.catalog_title_id is not None:
            videos_by_title.setdefault(video.catalog_title_id, []).append(video)
        if video.catalog_collection_id is not None:
            videos_by_collection_id.setdefault(
                video.catalog_collection_id, []
            ).append(video)
    session.expire_all()
    for collection in session.scalars(select(CatalogCollection).options(
        selectinload(CatalogCollection.titles).selectinload(CatalogTitle.metadata_record)
    )).all():
        collection_videos = videos_by_collection_id.get(collection.id)
        if collection_videos is None:
            recalculate_collection_numbering(collection, videos_by_title)
            continue
        if collection.id in manual_split_status_locked:
            recalculate_collection_numbering(collection, videos_by_title)
            continue
        finalize_collection_hierarchy(collection, collection_videos)
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
    ffprobe_timeout_seconds: float = 60,
    library_access_timeout_seconds: float = 10,
    library_healthcheck_interval_files: int = 25,
) -> ScanResult:
    try:
        return _scan_library(
            session,
            root,
            require_mount=require_mount,
            confirm_deletions=confirm_deletions,
            ffprobe_timeout_seconds=ffprobe_timeout_seconds,
            library_access_timeout_seconds=library_access_timeout_seconds,
            library_healthcheck_interval_files=library_healthcheck_interval_files,
        )
    except Exception:
        session.rollback()
        logger.exception("Sken byl vrácen zpět kvůli neočekávané chybě")
        raise
