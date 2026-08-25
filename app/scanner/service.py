from __future__ import annotations

from dataclasses import dataclass
from collections import defaultdict
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
from app.hierarchy_authority import manual_hierarchy_snapshot_requires_preservation
from app.hierarchy_evaluation import finalize_collection_hierarchy
from app.hierarchy_review import extract_local_period_hint
from app.manual_split import (
    apply_manual_split_decisions,
    evaluate_persisted_manual_split,
    historical_manual_split_ambiguities,
    manual_split_titles,
    persisted_manual_split_authority_collections,
)
from app.models import (
    AudioTrack, CatalogCollection, CatalogTitle, ExternalSubtitle, InternalSubtitle,
    UnresolvedExternalSubtitle, Video,
)
from app.numbering import recalculate_collection_numbering
from app.probe import ProbeError, probe_video
from app.subtitles import SUBTITLE_EXTENSIONS, read_and_detect, safe_subtitle_matches

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


def iter_external_subtitles(root: Path):
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
            if path.suffix.lower() in SUBTITLE_EXTENSIONS:
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


def _sync_external_subtitles(
    session: Session, library_root: Path, videos: list[Video]
) -> None:
    """Account for every physical subtitle once while preserving manual authority."""
    session.flush()
    video_by_relative = {video.relative_path: video for video in videos}
    videos_by_parent: dict[Path, list[Path]] = defaultdict(list)
    for relative_path in video_by_relative:
        absolute_path = library_root / relative_path
        videos_by_parent[absolute_path.parent].append(absolute_path)

    linked_by_path: dict[str, list[ExternalSubtitle]] = defaultdict(list)
    for subtitle in session.scalars(select(ExternalSubtitle)).all():
        linked_by_path[subtitle.relative_path].append(subtitle)
    unresolved_by_path = {
        subtitle.relative_path: subtitle
        for subtitle in session.scalars(select(UnresolvedExternalSubtitle)).all()
    }

    physical_paths = list(iter_external_subtitles(library_root))
    seen = {path.relative_to(library_root).as_posix() for path in physical_paths}
    for relative_path, rows in linked_by_path.items():
        if relative_path not in seen:
            for row in rows:
                session.delete(row)
    for relative_path, row in unresolved_by_path.items():
        if relative_path not in seen:
            session.delete(row)

    for path in physical_paths:
        relative_path = path.relative_to(library_root).as_posix()
        language = read_and_detect(path)
        data = {
            "relative_path": relative_path,
            "filename": path.name,
            "extension": path.suffix.lower(),
            "codec": path.suffix.lower().lstrip("."),
            "language": language,
            "normalized_language": normalize_language(language),
        }
        linked = linked_by_path.get(relative_path, [])
        manual = [row for row in linked if row.match_method == "manual"]
        unresolved = unresolved_by_path.get(relative_path)

        if manual:
            keep = min(manual, key=lambda row: row.id or 0)
            keep.codec = data["codec"]
            keep.language = data["language"]
            keep.normalized_language = data["normalized_language"]
            for row in linked:
                if row is not keep:
                    session.delete(row)
            if unresolved is not None:
                session.delete(unresolved)
            continue

        if unresolved is not None and unresolved.status == "confirmed_no_match":
            unresolved.filename = data["filename"]
            unresolved.extension = data["extension"]
            unresolved.language = data["language"]
            unresolved.normalized_language = data["normalized_language"]
            for row in linked:
                session.delete(row)
            continue

        match_method, candidates = safe_subtitle_matches(
            videos_by_parent.get(path.parent, []), path,
        )
        if len(candidates) == 1:
            video = video_by_relative[candidates[0].relative_to(library_root).as_posix()]
            keep = next((row for row in linked if row.video_id == video.id), None)
            if keep is None and linked:
                keep = min(linked, key=lambda row: row.id or 0)
            if keep is None:
                keep = ExternalSubtitle(video_id=video.id, relative_path=relative_path)
                session.add(keep)
            keep.video_id = video.id
            keep.codec = data["codec"]
            keep.language = data["language"]
            keep.normalized_language = data["normalized_language"]
            keep.match_method = "automatic"
            for row in linked:
                if row is not keep:
                    session.delete(row)
            if unresolved is not None:
                session.delete(unresolved)
            logger.debug(
                "Externí titulek %s bezpečně přiřazen (%s) k %s",
                relative_path, match_method, video.relative_path,
            )
            continue

        for row in linked:
            session.delete(row)
        if unresolved is None:
            unresolved = UnresolvedExternalSubtitle(
                relative_path=relative_path,
                filename=data["filename"],
                extension=data["extension"],
            )
            session.add(unresolved)
        unresolved.filename = data["filename"]
        unresolved.extension = data["extension"]
        unresolved.language = data["language"]
        unresolved.normalized_language = data["normalized_language"]
        unresolved.status = "unresolved"


def _sync_audio_tracks(
    session: Session, video: Video, track_data: list[dict]
) -> None:
    """Preserve manual authority while refreshing ffprobe data by stream index."""
    with session.no_autoflush:
        existing = {track.stream_index: track for track in video.audio_tracks}
        incoming = {int(track["stream_index"]): track for track in track_data}

        for stream_index, data in incoming.items():
            track = existing.get(stream_index)
            if track is None:
                video.audio_tracks.append(AudioTrack(**data))
            else:
                track.codec = data.get("codec")
                track.language = data.get("language") or "unknown"

        for stream_index, track in existing.items():
            if stream_index not in incoming:
                video.audio_tracks.remove(track)


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
                _sync_audio_tracks(session, video, metadata["audio"])
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
    _sync_external_subtitles(session, root, current_videos)
    hierarchy = derive_library_hierarchy([video.relative_path for video in current_videos])
    collections = {
        value.relative_root_path: value
        for value in session.scalars(select(CatalogCollection)).all()
    }
    titles = {
        value.relative_root_path: value
        for value in session.scalars(select(CatalogTitle)).all()
    }
    protected_collection_paths: set[str] = set()
    for video in current_videos:
        authority_collections = persisted_manual_split_authority_collections(video)
        if video.manual_split_rule_videos:
            authority_is_valid = (
                len(authority_collections) == 1
                and all(
                    link.catalog_title is not None
                    and link.catalog_title.collection is authority_collections[0]
                    for link in video.manual_split_rule_videos
                )
            )
            if authority_is_valid:
                video.catalog_collection = authority_collections[0]
            else:
                protected_collection_paths.update(
                    collection.relative_root_path
                    for collection in authority_collections
                )
                if video.catalog_collection is not None:
                    protected_collection_paths.add(
                        video.catalog_collection.relative_root_path
                    )
                logger.warning(
                    "Video %s má nekonzistentní persistentní manual-split authority; "
                    "scanner jeho hierarchy assignment nemění.",
                    video.relative_path,
                )
            continue
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
        legacy_conflict_collection = (
            video.catalog_collection
            if video.catalog_title is None
            and video.catalog_collection is not None
            and video.catalog_collection.hierarchy_status == "conflict"
            and manual_split_titles(video.catalog_collection)
            else None
        )
        if legacy_conflict_collection is not None:
            video.catalog_collection = legacy_conflict_collection
            continue
        assigned_manual_title = (
            video.catalog_title
            if video.catalog_title is not None
            and manual_hierarchy_snapshot_requires_preservation(video.catalog_title)
            and video.catalog_title.collection is not None
            else None
        )
        path_title = titles.get(title_data.relative_root_path)
        reassigned_path_title = (
            path_title
            if path_title is not None
            and manual_hierarchy_snapshot_requires_preservation(path_title)
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
        if not manual_hierarchy_snapshot_requires_preservation(catalog_title):
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
        if video.catalog_collection is not None:
            videos_by_collection_path.setdefault(
                video.catalog_collection.relative_root_path, []
            ).append(video)
    for path, collection_videos in videos_by_collection_path.items():
        collection = collections[path]
        collection.local_period_hint = extract_local_period_hint(collection.local_title)
        if manual_split_titles(collection):
            manual_split = evaluate_persisted_manual_split(
                collection,
                collection_videos,
            )
            if historical_manual_split_ambiguities(collection, manual_split):
                protected_collection_paths.add(path)
                continue
            apply_manual_split_decisions(manual_split, collection)
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
        if collection.relative_root_path in protected_collection_paths:
            continue
        collection_videos = videos_by_collection_id.get(collection.id)
        if collection_videos is None:
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
