"""Small local archive decision sidecars; no SQLite or library writes."""
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path, PurePosixPath

from .target_planner import relative_locator_safe, source_collection, technical_root
from .target_planner_types import ArchiveReviewDecision, SideAssetEvidence


class ArchiveReviewConflict(ValueError):
    pass


class ArchiveReviewUnavailable(OSError):
    def __init__(self):
        super().__init__('Nelze načíst lokální rozhodnutí Archive Review. Nevyřešené položky zůstávají REVIEW; '
                         'ověřte přístup k evidenci a vytvořte nový náhled.')


def decision_directory(database):
    database = Path(database)
    return database.with_name(database.name + '.archive-reviews')


def _decision_path(database, source):
    return decision_directory(database) / (hashlib.sha256(source.encode('utf-8', 'surrogatepass')).hexdigest() + '.json')


def load_archive_decisions(database):
    directory = decision_directory(database)
    from .target_planner_filesystem import validate_inventory_root
    try:
        validate_inventory_root(directory)
        paths = sorted(directory.iterdir())
    except FileNotFoundError:
        return ()
    except OSError as exc:
        raise ArchiveReviewUnavailable() from exc
    result = []
    for path in paths:
        if path.suffix != '.json':
            continue
        try:
            validate_inventory_root(path)
            raw = json.loads(path.read_text(encoding='utf-8'))
        except OSError as exc:
            raise ArchiveReviewUnavailable() from exc
        decision = ArchiveReviewDecision(**raw)
        if (not relative_locator_safe(decision.source)
                or type(decision.size) is not int or decision.size < 0
                or type(decision.mtime_ns) is not int or decision.mtime_ns < 0
                or type(decision.collection_id) is not int
                or decision.choice not in {'secondary', 'standalone'}
                or path != _decision_path(database, decision.source)):
            raise ValueError('Neplatný lokální záznam rozhodnutí o archivu.')
        if decision.choice == 'secondary' and (
                type(decision.secondary_video_id) is not int or type(decision.primary_video_id) is not int
                or not isinstance(decision.secondary_source, str) or not isinstance(decision.primary_source, str)):
            raise ValueError('Neplatná explicitní vazba archivu na video.')
        result.append(decision)
    return tuple(result)


def persist_archive_decision(database, library_root, source, decision):
    # Unlike exported manifests, appdata may live in the repo's ignored data/.
    # Reuse the existing filesystem checks and atomic writer without requiring
    # the manifest export location policy for application state.
    from .tools.execution_manifest import _atomic_local_write, _mount_fstype
    from .target_planner_filesystem import validate_inventory_root
    directory = decision_directory(database)
    path = _decision_path(database, source)
    validate_inventory_root(path)
    library = Path(library_root).resolve()
    if library == directory.resolve() or library in directory.resolve().parents:
        raise ValueError('Rozhodnutí musí být v lokální appdata mimo knihovnu.')
    fstype = _mount_fstype(Path(database).absolute().parent)
    if fstype is None or fstype in {'cifs', 'smb3', 'nfs', 'nfs4', '9p'} or fstype.startswith(('fuse.sshfs', 'fuse.rclone')):
        raise ValueError('Rozhodnutí vyžaduje lokální appdata filesystem.')
    if decision is None:
        if directory.exists():
            path.unlink(missing_ok=True)
        return
    directory.mkdir(mode=0o700, exist_ok=True)
    _atomic_local_write(path, json.dumps(asdict(decision), ensure_ascii=True, sort_keys=True) + '\n')


def resolve_archive_decisions(context, snapshot):
    """Translate decisions into existing evidence; stale authority stays REVIEW."""
    if not context.archive_decisions:
        return context
    inventory = {e.relative_path: e for e in snapshot.entries} if snapshot else {}
    videos = {v.id: v for v in context.videos}
    sides = list(context.side_assets)
    for decision in context.archive_decisions:
        entry = inventory.get(decision.source)
        error = None
        if entry is None or entry.kind != 'regular' or (entry.size, entry.mtime_ns) != (decision.size, decision.mtime_ns):
            error = 'archive_review_source_changed'
        elif (technical_root(decision.source)
                or PurePosixPath(decision.source).suffix.lower() not in {'.zip', '.rar', '.7z'}
                or source_collection(context, decision.source) != decision.collection_id):
            error = 'archive_review_context_changed'
        if decision.choice == 'secondary' and error is None:
            secondary = videos.get(decision.secondary_video_id)
            primary = videos.get(decision.primary_video_id)
            if (not secondary or not primary or secondary.duplicate_validity != 'valid'
                    or secondary.duplicate_primary_id != primary.id
                    or secondary.collection_id != decision.collection_id
                    or secondary.source != decision.secondary_source or primary.source != decision.primary_source):
                error = 'archive_review_duplicate_changed'
        sides.append(SideAssetEvidence(decision.source, decision.secondary_video_id or 0,
            error or ('explicit_human_secondary_archive' if decision.choice == 'secondary' else 'explicit_human_standalone_archive'),
            expected_size=-1 if error else decision.size,
            object_kind='subtitle_archive' if decision.choice == 'standalone' and not error else 'duplicate_side_asset'))
    return replace(context, side_assets=tuple(sides), archive_decisions=())


def _new_decision(context, entry, choice, secondary_id=None):
    collection_id = source_collection(context, entry.relative_path)
    if (collection_id is None or technical_root(entry.relative_path)
            or PurePosixPath(entry.relative_path).suffix.lower() not in {'.zip', '.rar', '.7z'}):
        raise ArchiveReviewConflict('Tento zdroj nelze řešit jako archiv v reorganizaci.')
    videos = {v.id: v for v in context.videos}
    secondary = videos.get(secondary_id)
    if choice == 'secondary':
        if not secondary or secondary.duplicate_validity != 'valid' or secondary.collection_id != collection_id:
            raise ArchiveReviewConflict('Vyberte konkrétní potvrzené duplicitní video z nabídky.')
        primary = videos.get(secondary.duplicate_primary_id)
        if not primary:
            raise ArchiveReviewConflict('Primární video duplicitní vazby již není dostupné.')
        return ArchiveReviewDecision(entry.relative_path, entry.size, entry.mtime_ns, collection_id,
            choice, secondary.id, secondary.source, primary.id, primary.source)
    if choice != 'standalone' or collection_id is None:
        raise ArchiveReviewConflict('Tuto položku nelze vyřešit jako samostatný archiv.')
    return ArchiveReviewDecision(entry.relative_path, entry.size, entry.mtime_ns, collection_id, choice)


def _with_decision(context, source, decision):
    return replace(context, archive_decisions=tuple(d for d in context.archive_decisions if d.source != source)
                   + ((decision,) if decision else ()))


def archive_review_rows(context, snapshot, plan):
    """Only unresolved duplicate archives and previously reviewed archives get a form."""
    from .target_planner_filesystem import plan_library
    inventory = {e.relative_path: e for e in snapshot.entries}
    saved = {d.source: d for d in context.archive_decisions}
    videos = {v.id: v for v in context.videos}
    candidates_by_collection = {}
    for video in context.videos:
        primary = videos.get(video.duplicate_primary_id)
        if video.duplicate_validity == 'valid' and primary:
            candidates_by_collection.setdefault(video.collection_id, []).append({
                'id': video.id, 'source': video.source, 'primary_id': primary.id, 'primary_source': primary.source})
    result = {}
    for row in plan.records:
        source = row.source_relative_path
        if source not in saved and 'duplicate_side_asset_association_unresolved' not in row.blockers:
            continue
        entry = inventory.get(source)
        candidates = candidates_by_collection.get(source_collection(context, source), [])
        decision = asdict(saved[source]) if source in saved else None
        fingerprint = hashlib.sha256(json.dumps([asdict(entry) if entry else None, candidates, decision],
            ensure_ascii=True, sort_keys=True).encode()).hexdigest()
        standalone_allowed = False
        standalone_reason = 'Zdrojový archiv již není dostupný.'
        if entry and entry.kind == 'regular' and not technical_root(source) and source_collection(context, source) is not None:
            proposed = _new_decision(context, entry, 'standalone')
            candidate_plan = plan_library(_with_decision(context, source, proposed), snapshot)
            candidate = next(r for r in candidate_plan.records if r.source_relative_path == source)
            standalone_allowed = candidate.status == 'READY'
            standalone_reason = '' if standalone_allowed else 'Cíl v Subs není platný nebo koliduje: ' + ', '.join(candidate.blockers)
        result[source] = {'fingerprint': fingerprint, 'candidates': candidates, 'decision': decision,
            'standalone_allowed': standalone_allowed, 'standalone_reason': standalone_reason}
    return result


def apply_archive_review(database, context, snapshot, plan, request):
    """Validate a fresh form and proposed target with the ordinary planner, then persist."""
    from .target_planner_filesystem import plan_library
    source = request.get('source')
    reviews = archive_review_rows(context, snapshot, plan)
    current = reviews.get(source)
    if current is None or request.get('fingerprint') != current['fingerprint']:
        raise ArchiveReviewConflict('Položka nebo rozhodnutí se změnily. Připravte nový náhled a zvolte řešení znovu.')
    choice = request.get('choice')
    if choice == 'unresolved':
        decision = None
    else:
        if choice == 'standalone' and not current['standalone_allowed']:
            raise ArchiveReviewConflict(current['standalone_reason'])
        entry = next((e for e in snapshot.entries if e.relative_path == source and e.kind == 'regular'), None)
        if entry is None:
            raise ArchiveReviewConflict('Archiv není dostupný jako běžný soubor.')
        try:
            secondary_id = int(request.get('secondary_video_id') or 0)
        except (ValueError, TypeError) as exc:
            raise ArchiveReviewConflict('Neplatné ID duplicitního videa.') from exc
        decision = _new_decision(context, entry, choice, secondary_id)
    proposed_context = _with_decision(context, source, decision)
    proposed_plan = plan_library(proposed_context, snapshot)
    if decision:
        candidate = next(r for r in proposed_plan.records if r.source_relative_path == source)
        if candidate.status != 'READY':
            raise ArchiveReviewConflict('Rozhodnutí nemá platný cíl bez kolize: ' + ', '.join(candidate.blockers))
    persist_archive_decision(database, snapshot.root, source, decision)
    return proposed_context, proposed_plan
