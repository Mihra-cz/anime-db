"""Read-only inventory, preservation lanes and full target namespace preflight."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat

from .physical_naming_components import (
    collision_keys, component_preview, evaluate_windows_path_budget,
)
from .target_planner import project_targets, relative_locator_safe, root_targets, source_collection, technical_root
from .target_planner_types import (
    FilesystemEntry, FilesystemSnapshot, PlanRecord, PlannerContext, SideAssetAccounting,
    TargetCollision, TargetPlan,
)

_ARCHIVES = frozenset({'.zip', '.rar', '.7z'})
_MEDIA = frozenset({'.mkv', '.mp4', '.m4v', '.avi', '.ass', '.ssa', '.srt', '.sub', '.vtt', '.idx'})


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode('utf-8')).hexdigest()


def validate_inventory_root(root: Path) -> Path:
    """Reject system bins and symlinked root ancestors before any inventory I/O."""
    root = Path(root).absolute()
    if any(part.casefold() == '#recycle' for part in root.parts):
        raise ValueError('library root is inside #recycle')
    cursor = Path(root.anchor)
    for part in root.parts[1:]:
        cursor /= part
        try:
            if stat.S_ISLNK(cursor.lstat().st_mode):
                raise ValueError('library root traverses a symlink')
        except FileNotFoundError:
            break
    return root


def inventory_filesystem(root: Path) -> FilesystemSnapshot:
    """Exactly one traversal, lstat semantics, no links or recycle-bin descent.

    Only source archive files are read for byte-identical-copy evidence. Videos,
    subtitles and other assets need stat only. Inaccessible paths stay explicit.
    """
    root = validate_inventory_root(root)
    entries = []
    diagnostics = []
    limits = []
    for name in ('PC_NAME_MAX', 'PC_PATH_MAX'):
        try:
            value = os.pathconf(root, name)
            limits.append(value if value > 0 else None)
        except (OSError, ValueError):
            limits.append(None)
            diagnostics.append(name.lower() + '_not_checked')

    def walk(directory):
        try:
            with os.scandir(directory) as stream:
                children = sorted(stream, key=lambda e: e.name)
        except OSError as error:
            rel = directory.relative_to(root).as_posix()
            entries.append(FilesystemEntry(rel, 'inaccessible', diagnostic=f'listing_error:{error.errno}'))
            return
        for child in children:
            rel = Path(child.path).relative_to(root).as_posix()
            try:
                info = child.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    # lstat identifies the directory, with no descent, listing
                    # or ACL bypass inside the Synology system lane.
                    if child.name.casefold() == '#recycle':
                        entries.append(FilesystemEntry(rel, 'system_excluded'))
                        continue
                    entries.append(FilesystemEntry(rel, 'directory', mtime_ns=info.st_mtime_ns))
                    walk(Path(child.path))
                elif stat.S_ISREG(info.st_mode):
                    digest = None
                    if Path(child.name).suffix.lower() in _ARCHIVES and technical_root(rel) != 'Subs':
                        # O_NOFOLLOW also protects a leaf swapped after lstat.
                        descriptor = os.open(child.path, os.O_RDONLY | os.O_NOFOLLOW)
                        with os.fdopen(descriptor, 'rb') as archive:
                            current = os.fstat(archive.fileno())
                            if not stat.S_ISREG(current.st_mode):
                                raise OSError('archive_no_longer_regular')
                            digest = hashlib.file_digest(archive, 'sha256').hexdigest()
                            after = os.fstat(archive.fileno())
                            if (current.st_size, current.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                                raise OSError('archive_changed_during_inventory')
                        if (info.st_size, info.st_mtime_ns) != (current.st_size, current.st_mtime_ns):
                            raise OSError('archive_changed_during_inventory')
                    entries.append(FilesystemEntry(rel, 'regular', info.st_size, info.st_mtime_ns, content_sha256=digest))
                else:
                    entries.append(FilesystemEntry(rel, 'symlink' if stat.S_ISLNK(info.st_mode) else 'nonregular'))
            except OSError as error:
                entries.append(FilesystemEntry(rel, 'inaccessible', diagnostic=f'stat_or_read_error:{error.errno}'))
    try:
        if not stat.S_ISDIR(root.lstat().st_mode):
            raise OSError('library_root_not_directory')
        walk(root)
    except OSError as error:
        entries.append(FilesystemEntry('.', 'inaccessible', diagnostic=str(error)))
    return FilesystemSnapshot(str(root), tuple(sorted(entries, key=lambda e: e.relative_path)), *limits, tuple(diagnostics))


def _diagnose(record, code, *, status='BLOCKED'):
    priority = {'READY': 0, 'WARNING': 1, 'REVIEW': 2, 'BLOCKED': 3, 'SYSTEM_EXCLUDED': 4}
    return replace(record, status=max((record.status, status), key=priority.get),
                   blockers=tuple(sorted(set(record.blockers + (code,)))))


def _duplicate_archive_evidence(context, entry, inventory, video_records, by_parent):
    members = by_parent.get(str(PurePosixPath(entry.relative_path).parent), ())
    if not members or not all(v.duplicate_primary_id is not None for v in members):
        return None, False
    targets = set()
    for video in members:
        row = video_records[video.id]
        if not row.duplicate or row.duplicate.validity != 'valid' or not row.target_relative_path:
            return None, True
        primary_parent = PurePosixPath(row.duplicate.primary_source_relative_path).parent
        primary_archive = inventory.get(str(primary_parent / PurePosixPath(entry.relative_path).name))
        if (entry.content_sha256 is None or primary_archive is None
            or primary_archive.kind != 'regular' or primary_archive.content_sha256 != entry.content_sha256):
            return None, True
        targets.add(str(PurePosixPath(row.target_relative_path).parent))
    return (members[0].id if len(targets) == 1 else None), True


def _side_evidence_matches(side, entry, inventory):
    """Validate carried identity/stat facts without rehashing archives in Subs."""
    if (entry is None or entry.kind != 'regular'
        or not isinstance(side.provenance, str) or not side.provenance.strip()
        or (side.expected_size is not None and side.expected_size != entry.size)):
        return False
    preserved_archive = side.object_kind == 'subtitle_archive' and technical_root(side.source) == 'Subs'
    if side.expected_sha256 is not None and not preserved_archive and side.expected_sha256 != entry.content_sha256:
        return False
    derived_copy = 'exclusive_secondary_directory_and_identical_primary_archive' in side.provenance.split(';')
    if derived_copy or side.primary_archive_source is not None or side.primary_archive_sha256 is not None:
        primary = inventory.get(side.primary_archive_source)
        return bool(primary and primary.kind == 'regular' and side.expected_sha256
            and side.primary_archive_sha256 == side.expected_sha256
            and side.primary_archive_source != side.source
            and PurePosixPath(side.primary_archive_source).name == PurePosixPath(side.source).name
            and (side.expected_size is None or primary.size == side.expected_size)
            and (primary.content_sha256 is None or primary.content_sha256 == side.primary_archive_sha256))
    return True


def _extra_records(context, snapshot, managed):
    roots = root_targets(context)
    inventory = {e.relative_path: e for e in snapshot.entries}
    by_parent = defaultdict(list)
    for video in context.videos:
        by_parent[str(PurePosixPath(video.source).parent)].append(video)
    video_records = {r.object_id: r for r in managed if r.object_kind == 'video'}
    evidence = defaultdict(list)
    carried = tuple(side for item in context.execution_evidence for side in item.known_side_assets)
    for side in dict.fromkeys(context.side_assets + carried):
        if side.object_kind != 'duplicate_side_asset':
            continue
        evidence[side.source].append(side)
    taken = {r.source_relative_path for r in managed}
    result = []
    for entry in snapshot.entries:
        source = entry.relative_path
        if source in taken or entry.kind == 'directory':
            continue
        if entry.kind == 'system_excluded':
            continue
        if entry.kind != 'regular':
            result.append(PlanRecord('unknown', source, source, None, 'REVIEW', 'REVIEW', blockers=(entry.diagnostic or entry.kind,), authority=('filesystem_snapshot',)))
            continue
        if source == 'seznam-souboru.txt':
            result.append(PlanRecord('keep_root', source, source, source, 'KEEP', 'READY', authority=('approved_root_txt_keep',)))
            continue
        collection_id = source_collection(context, source)
        root = roots.get(collection_id, (None, (), 'unavailable'))[0]
        suffix = PurePosixPath(source).suffix.lower()
        side = evidence.get(source, ())
        secondary_id = side[0].secondary_video_id if len(side) == 1 else None
        possible_duplicate = bool(side)
        provenance = tuple(item.provenance for item in side)
        if not side and suffix in _ARCHIVES and technical_root(source) != 'Subs':
            secondary_id, possible_duplicate = _duplicate_archive_evidence(context, entry, inventory, video_records, by_parent)
            provenance = ('exclusive_secondary_directory_and_identical_primary_archive',) if secondary_id else ()
        secondary = video_records.get(secondary_id)
        evidence_matches = all(_side_evidence_matches(item, entry, inventory) for item in side)
        if side and technical_root(source) == 'Subs':
            result.append(PlanRecord('duplicate_quarantine_candidate', source, source, None, 'REVIEW', 'REVIEW',
                blockers=('side_asset_evidence_conflicts_with_subs_lane',), authority=provenance,
                duplicate=secondary.duplicate if secondary else None))
        elif possible_duplicate:
            target = str(PurePosixPath(secondary.target_relative_path).parent / PurePosixPath(source).name) if evidence_matches and secondary and secondary.target_relative_path and secondary.duplicate and secondary.duplicate.validity == 'valid' else None
            result.append(PlanRecord('duplicate_side_asset' if target else 'duplicate_quarantine_candidate', source, source, target,
                'KEEP' if target == source else 'QUARANTINE' if target else 'REVIEW', 'READY' if target else 'REVIEW',
                blockers=() if target else ('duplicate_side_asset_evidence_mismatch' if not evidence_matches else 'duplicate_side_asset_association_unresolved',), authority=provenance,
                collection_id=collection_id, duplicate=secondary.duplicate if secondary else None))
        elif technical_root(source):
            # The flat archive lane has no anime owner. Quarantine paths alone
            # never account for a file or grant purge authority.
            parts = PurePosixPath(source).parts
            if parts[0] == 'Subs' and len(parts) == 2 and suffix in _ARCHIVES:
                result.append(PlanRecord('subtitle_archive', source, source, source, 'KEEP', 'READY',
                    info=('archive_source_preserved_contents_not_classified',),
                    authority=('source_archive_extension', 'reserved_subs_lane')))
            else:
                result.append(PlanRecord('unknown', source, source, None, 'REVIEW', 'REVIEW',
                    blockers=('unprovenanced_or_unexpected_technical_lane_asset',), authority=('filesystem_snapshot',)))
        elif suffix in _ARCHIVES and collection_id is not None:
            result.append(PlanRecord('subtitle_archive', source, source, str(PurePosixPath('Subs') / PurePosixPath(source).name),
                'MOVE', 'READY', info=('archive_source_preserved_contents_not_classified',), authority=('source_archive_extension',)))
        elif root and suffix not in _MEDIA:
            owner = next(c for c in context.collections if c.id == collection_id)
            locators = [p for p in owner.locators if source.startswith(p + '/')]
            # Strip known structural source containers, retain actual bundle text.
            locators.extend(t.source_locator for t in context.titles if t.collection_id == collection_id
                            and t.kind in {'season', 'part', 'cour', 'title'} and t.source_locator
                            and source.startswith(t.source_locator + '/'))
            locator = max(locators, key=lambda p: len(PurePosixPath(p).parts))
            subtree = PurePosixPath(source).relative_to(locator)
            parts = subtree.parts[1:] if collision_keys(subtree.parts[0]).fold_key == 'extras' else subtree.parts
            target = str(PurePosixPath(root, 'Extras', *parts))
            namespace = str(PurePosixPath(root, 'Extras', parts[0])) if len(parts) > 1 else None
            result.append(PlanRecord('auxiliary', source, source, target, 'KEEP' if target == source else 'MOVE', 'READY',
                authority=(f'collection:{collection_id}', 'explicit_source_locator', 'preserved_subtree'),
                collection_id=collection_id, auxiliary_namespace=namespace))
        else:
            result.append(PlanRecord('unknown', source, source, None, 'REVIEW', 'REVIEW',
                blockers=('unmanaged_or_ambiguous_asset',), authority=('filesystem_snapshot',), collection_id=collection_id))
    # Carried/explicit known assets must not vanish when inventory cannot see
    # them, including DB-known locators inside a system-excluded directory.
    represented = taken | {r.source_relative_path for r in result}
    for side in dict.fromkeys(context.side_assets + carried):
        if side.source not in represented:
            result.append(PlanRecord(side.object_kind, side.object_id if side.object_id is not None else side.source,
                side.source, None, 'REVIEW', 'REVIEW', blockers=('known_side_asset_not_in_inventory',),
                authority=(side.provenance,)))
            represented.add(side.source)
    return result


def _namespace_collisions(records, directories=()):
    nodes = {guard: defaultdict(list) for guard in ('exact', 'casefold', 'uppercase')}
    for index, record in enumerate(records):
        if record.status == 'SYSTEM_EXCLUDED':
            continue
        path = record.target_relative_path or record.source_relative_path
        if not relative_locator_safe(path):
            continue
        components = PurePosixPath(path).parts
        for length in range(1, len(components)+1):
            raw = '/'.join(components[:length])
            keys = [collision_keys(p) for p in components[:length]]
            for guard, attr in [('exact', 'exact_key'), ('casefold', 'fold_key'), ('uppercase', 'uppercase_guard')]:
                key = tuple(getattr(k, attr) for k in keys)
                nodes[guard][key].append((raw, length == len(components), index))
    # Existing empty directories occupy the namespace too. They are context,
    # not file actions; exact approved parent directories may be reused.
    for path in directories:
        if not relative_locator_safe(path):
            continue
        components = PurePosixPath(path).parts
        for length in range(1, len(components)+1):
            raw = '/'.join(components[:length])
            keys = [collision_keys(p) for p in components[:length]]
            for guard, attr in [('exact', 'exact_key'), ('casefold', 'fold_key'), ('uppercase', 'uppercase_guard')]:
                key = tuple(getattr(k, attr) for k in keys)
                nodes[guard][key].append((raw, False, None))
    collisions = []
    for guard, groups in nodes.items():
        for members in groups.values():
            if not any(index is not None for _, _, index in members):
                continue
            paths = {raw for raw, _, _ in members}
            leaves = {index for _, leaf, index in members if leaf}
            parents = [item for item in members if not item[1]]
            if leaves and parents:
                reason = 'file_directory'
            elif len(leaves) > 1 or len(paths) > 1:
                reason = guard
            else:
                continue
            indices = sorted({index for _, _, index in members if index is not None})
            context_keys = sorted({('filesystem_directory', raw) for raw, _, index in members if index is None})
            collision = TargetCollision(reason, tuple(sorted(paths)),
                tuple((records[i].object_kind, records[i].object_id) for i in indices) + tuple(context_keys))
            if collision not in collisions:
                collisions.append(collision)
            for index in indices:
                records[index] = _diagnose(records[index], 'target_collision:' + reason)
    # Separate collection IDs may never merge into one physical anime root,
    # even when their filenames do not collide. Shared Season owners may.
    root_owners = defaultdict(list)
    for index, record in enumerate(records):
        if record.collection_id is not None and record.target_relative_path and not record.target_relative_path.startswith('Duplicates/'):
            root_owners[PurePosixPath(record.target_relative_path).parts[0]].append(index)
    for root, indices in root_owners.items():
        if len({records[i].collection_id for i in indices}) > 1:
            collisions.append(TargetCollision('collection_root', (root,), tuple((records[i].object_kind, records[i].object_id) for i in indices)))
            for i in indices:
                records[i] = _diagnose(records[i], 'collection_root_collision')
    managed = [r for r in records if r.object_kind in {'video', 'subtitle'} and r.target_relative_path and r.duplicate is None]
    for index, record in enumerate(records):
        if record.auxiliary_namespace:
            key = tuple(collision_keys(p).fold_key for p in PurePosixPath(record.auxiliary_namespace).parts)
            owners = [r for r in managed if tuple(collision_keys(p).fold_key for p in PurePosixPath(r.target_relative_path).parent.parts[:len(key)]) == key]
            if owners:
                records[index] = _diagnose(record, 'auxiliary_managed_namespace_collision', status='REVIEW')
                collisions.append(TargetCollision('auxiliary_namespace', (record.auxiliary_namespace,), tuple(
                    [(record.object_kind, record.object_id)] + [(r.object_kind, r.object_id) for r in owners])))
    return tuple(sorted(collisions, key=lambda c: (c.guard, c.target_paths, repr(c.record_keys))))


def plan_library(context: PlannerContext, snapshot: FilesystemSnapshot | None = None, *, windows_root: str | None = None) -> TargetPlan:
    """Combine pure domain targets with optional coverage and preflight evidence."""
    if windows_root is not None:
        evaluate_windows_path_budget(windows_root)
    records = list(project_targets(context))
    videos_by_id = {v.id: v for v in context.videos}
    carried = {}
    for evidence in context.execution_evidence:
        secondary = videos_by_id.get(evidence.secondary_video_id)
        if (evidence.secondary_video_id in carried or secondary is None
            or secondary.duplicate_primary_id != evidence.primary_video_id
            or any(s.secondary_video_id != evidence.secondary_video_id for s in evidence.known_side_assets)
            or not evidence.provenance or not all(isinstance(p, str) and p.strip() for p in evidence.provenance)):
            raise ValueError('Execution evidence has conflicting IDs or missing provenance.')
        carried[evidence.secondary_video_id] = evidence
    diagnostics = list(snapshot.diagnostics if snapshot else ('filesystem_not_checked',))
    excluded = tuple(e.relative_path for e in snapshot.entries if e.kind == 'system_excluded') if snapshot else ()
    def system_excluded(path):
        return (any(collision_keys(p).fold_key == '#recycle' for p in PurePosixPath(path).parts[:-1])
            or any(path == p or path.startswith(p + '/') for p in excluded))
    if snapshot:
        records.extend(_extra_records(context, snapshot, records))
        inventory = {e.relative_path: e for e in snapshot.entries}
    else:
        represented = {r.source_relative_path for r in records}
        for side in context.side_assets + tuple(s for item in context.execution_evidence for s in item.known_side_assets):
            if system_excluded(side.source) and side.source not in represented:
                records.append(PlanRecord(side.object_kind, side.object_id if side.object_id is not None else side.source,
                    side.source, None, 'REVIEW', 'REVIEW', authority=(side.provenance,)))
                represented.add(side.source)
    for i, row in enumerate(records):
        if system_excluded(row.source_relative_path):
            # A locator inside #recycle is outside the active library: keep the
            # row with the existing missing-file state, never a recovery target.
            records[i] = _diagnose(replace(row, target_relative_path=None, action='REVIEW'),
                                   'source_missing_or_not_regular')
    if snapshot:
        for i, row in enumerate(records):
            if system_excluded(row.source_relative_path):
                continue
            entry = inventory.get(row.source_relative_path)
            if entry is None or entry.kind != 'regular':
                records[i] = _diagnose(row, 'source_missing_or_not_regular') if row.object_kind not in {'unknown'} else row
        # No duplicate diagnostic ever grants purge permission from a snapshot.
        for i, row in enumerate(records):
            if row.duplicate:
                d = row.duplicate
                primary = inventory.get(d.primary_source_relative_path)
                physical = 'REGULAR_FILE' if primary and primary.kind == 'regular' else 'MISSING' if primary is None else primary.kind.upper()
                outside = bool(d.primary_source_relative_path and PurePosixPath(d.primary_source_relative_path).parts[0].casefold() != 'duplicates')
                d = replace(d, primary_physical_state=physical, primary_outside_duplicates=outside,
                    secondary_in_duplicates=PurePosixPath(row.source_relative_path).parts[0].casefold() == 'duplicates')
                row = replace(row, duplicate=d)
                if physical != 'REGULAR_FILE' or not outside:
                    row = _diagnose(row, 'duplicate_primary_physical_safety_gate')
                records[i] = row
        # DB-known subtitles bypass filesystem-only classification above, but
        # carried identity/stat/provenance is still execution evidence for them.
        by_source = defaultdict(list)
        for i, row in enumerate(records):
            by_source[row.source_relative_path].append(i)
        for evidence in context.execution_evidence:
            for side in evidence.known_side_assets:
                indices = by_source[side.source]
                matches = len(indices) == 1 and _side_evidence_matches(side, inventory.get(side.source), inventory)
                if matches:
                    row = records[indices[0]]
                    matches = row.object_kind == side.object_kind and (side.object_id is None or side.object_id == row.object_id)
                if not matches:
                    for i in indices:
                        records[i] = _diagnose(records[i], 'carried_side_asset_evidence_mismatch', status='REVIEW')
    collisions = _namespace_collisions(records, (e.relative_path for e in snapshot.entries if e.kind == 'directory') if snapshot else ())
    max_component = 0
    max_path = None
    windows_state = 'NOT_CHECKED / PRE_EXECUTION_REQUIRED' if windows_root is None else 'CHECKED'
    for i, row in enumerate(records):
        path = row.target_relative_path
        if not path:
            continue
        for part in PurePosixPath(path).parts:
            component = component_preview(part)
            max_component = max(max_component, component.utf8_bytes or 0)
            for diagnostic in component.diagnostics:
                if diagnostic.severity == 'error':
                    row = _diagnose(row, diagnostic.code)
            if snapshot and snapshot.name_max is not None and (component.utf8_bytes or 0) > snapshot.name_max:
                row = _diagnose(row, 'runtime_name_max')
        try:
            absolute_utf8 = str(PurePosixPath(snapshot.root if snapshot else '/') / path).encode('utf-8')
        except UnicodeEncodeError:
            records[i] = _diagnose(row, 'invalid_logical_input')
            continue
        if snapshot:
            size = len(absolute_utf8)
            max_path = max(max_path or 0, size)
            if snapshot.path_max is not None and size + 1 > snapshot.path_max:
                row = _diagnose(row, 'runtime_path_max')
        if windows_root is not None:
            absolute = str(PureWindowsPath(windows_root).joinpath(*PurePosixPath(path).parts))
            if not evaluate_windows_path_budget(absolute).within_target:
                row = replace(row, status='WARNING' if row.status == 'READY' else row.status,
                    warnings=tuple(sorted(set(row.warnings + ('windows_path_budget',)))))
                windows_state = 'WARNING'
        records[i] = row
    # Accounting consumes FINAL namespace/path diagnostics. An auxiliary
    # classification alone cannot prove ownership by a secondary duplicate.
    video_sources = {v.id: v.source for v in context.videos}
    records_by_source = defaultdict(list)
    for record in records:
        records_by_source[record.source_relative_path].append(record)
    for i, row in enumerate(records):
        if row.duplicate is None:
            continue
        d = row.duplicate
        secondary_source = video_sources.get(d.secondary_video_id)
        parent = str(PurePosixPath(secondary_source).parent) if secondary_source else None
        side_assets = []
        evidence = carried.get(d.secondary_video_id)
        # Relocation erases source-neighbour evidence. A quarantine directory
        # cannot reconstruct its historical completeness, even when empty.
        if evidence:
            state = evidence.side_asset_accounting
            provenance = evidence.provenance
        elif snapshot is None or parent in {None, '.'} or technical_root(secondary_source):
            state = SideAssetAccounting.UNKNOWN
            provenance = ()
        else:
            state = SideAssetAccounting.COMPLETE
            provenance = ('source_snapshot_accounting',)
        accounted = True
        for other in records:
            source = other.source_relative_path
            # A library-root secondary shares its directory with the whole library.
            if (parent is None or (parent != '.' and not source.startswith(parent + '/'))
                    or other.object_kind == 'video' or other.status == 'SYSTEM_EXCLUDED'):
                continue
            if other.status in {'REVIEW', 'BLOCKED'}:
                accounted = False
            elif other.duplicate and other.duplicate.validity == 'valid':
                # Another secondary's quarantined asset is accounted by its owner.
                if other.duplicate.secondary_video_id == d.secondary_video_id:
                    side_assets.append(source)
            elif other.object_kind == 'subtitle' and any(status in {'automatic_match', 'confirmed_compatible'} for _, status in other.compatibility):
                # Explicit positive compatibility accounts for an unrelated
                # primary's sidecar; it never transfers to this secondary.
                pass
            else:
                accounted = False
        if evidence:
            # Known source neighbours may have moved outside quarantine. Check
            # the carried identities/paths, rather than forgetting those files.
            for side in evidence.known_side_assets:
                matches = records_by_source[side.source]
                if (len(matches) != 1 or matches[0].status in {'REVIEW', 'BLOCKED'}
                    or matches[0].object_kind != side.object_kind
                    or (side.object_id is not None and matches[0].object_id != side.object_id)):
                    accounted = False
        if state == SideAssetAccounting.COMPLETE and (not accounted or snapshot is None or d.validity != 'valid'):
            state = SideAssetAccounting.INCOMPLETE if snapshot else SideAssetAccounting.UNKNOWN
        records[i] = replace(row, duplicate=replace(d, side_assets=tuple(sorted(side_assets)),
            side_asset_accounting=state, accounting_provenance=provenance))
    records = tuple(sorted(records, key=lambda r: (r.object_kind, str(r.object_id), r.source_relative_path)))
    counts = Counter(r.object_kind for r in records)
    counts['filesystem_visible_assets'] = sum(e.kind == 'regular' for e in snapshot.entries) if snapshot else 0
    counts['system_excluded_dirs'] = sum(e.kind == 'system_excluded' for e in snapshot.entries) if snapshot else 0
    states = Counter(r.status for r in records)
    for state in ('READY', 'WARNING', 'BLOCKED', 'REVIEW'):
        states.setdefault(state, 0)
    filesystem_hash = _hash(asdict(replace(snapshot, entries=tuple(sorted(snapshot.entries, key=lambda e:e.relative_path))))) if snapshot else None
    payload = dict(records=[asdict(r) for r in records], collisions=[asdict(c) for c in collisions],
        counts=sorted(counts.items()), status_counts=sorted(states.items()), filesystem_hash=filesystem_hash,
        name_max=snapshot.name_max if snapshot else None, path_max=snapshot.path_max if snapshot else None,
        max_component=max_component, max_path=max_path, windows_state=windows_state, diagnostics=sorted(diagnostics))
    return TargetPlan(records, collisions, tuple(sorted(counts.items())), tuple(sorted(states.items())), filesystem_hash,
        _hash(payload), snapshot.name_max if snapshot else None, snapshot.path_max if snapshot else None,
        max_component, max_path, windows_state, tuple(sorted(diagnostics)))
