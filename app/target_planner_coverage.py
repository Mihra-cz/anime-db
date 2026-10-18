"""Pre-reorganization accounting over existing planner evidence, with no I/O.

Completeness describes this planning report, never execution readiness or a
physically verified post-state. Directory review does not authorize cleanup.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import PurePosixPath

from .target_planner import relative_locator_safe
from .target_planner_types import FilesystemSnapshot, TargetPlan


@dataclass(frozen=True, order=True)
class CoverageIssue:
    path: str
    code: str


@dataclass(frozen=True)
class FileCoverage:
    path: str
    inventory_kind: str
    record_count: int
    disposition: str
    target: str | None
    object_kind: str | None
    object_id: int | str | None
    record_keys: tuple[tuple[str, int | str], ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class DirectoryCoverage:
    path: str
    disposition: str
    exists: bool | None
    empty: bool | None
    required_in_target: bool
    contents_inventoried: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class LibraryCoverage:
    library_root: str
    plan_hash: str
    filesystem_hash: str | None
    inventory_complete: bool
    files_complete: bool
    complete: bool
    counts: tuple[tuple[str, int], ...]
    files: tuple[FileCoverage, ...]
    directories: tuple[DirectoryCoverage, ...]
    issues: tuple[CoverageIssue, ...]
    scope: str = 'PRE_REORGANIZATION'
    execution_verified: bool = False


def _parents(path):
    return tuple(str(parent) for parent in PurePosixPath(path).parents if str(parent) != '.')


def report_coverage(snapshot: FilesystemSnapshot, plan: TargetPlan) -> LibraryCoverage:
    """Account for every observed source and directory without changing the plan.

    An inaccessible entry represents unknown contents, not one known file or an
    empty directory. Existing directories whose files will leave are candidates
    for human cleanup review only; neither deletion nor mkdir is performed here.
    """
    inventory, records = defaultdict(list), defaultdict(list)
    nonempty_directories = set()
    issues, inventory_issues, file_issues = set(), set(), set()
    uncertain = set()
    for entry in snapshot.entries:
        inventory[entry.relative_path].append(entry)
        nonempty_directories.update(_parents(entry.relative_path))
        if entry.kind not in {'regular', 'directory', 'system_excluded'}:
            code = 'inaccessible_inventory' if entry.kind == 'inaccessible' else 'unsupported_inventory_kind:' + entry.kind
            inventory_issues.add(CoverageIssue(entry.relative_path, code))
            uncertain.update((entry.relative_path, *_parents(entry.relative_path)))
    for diagnostic in sorted(set(snapshot.diagnostics + plan.diagnostics)):
        inventory_issues.add(CoverageIssue('.', 'inventory_diagnostic:' + diagnostic))
    for path, entries in inventory.items():
        # inventory_filesystem records a directory before attempting its listing.
        if len(entries) > 1 and sorted(e.kind for e in entries) != ['directory', 'inaccessible']:
            inventory_issues.add(CoverageIssue(path, 'duplicate_inventory_path'))
    for record in plan.records:
        records[record.source_relative_path].append(record)
    excluded = {path for path, entries in inventory.items() if any(e.kind == 'system_excluded' for e in entries)}
    excluded_parents = {parent for path in excluded for parent in _parents(path)}
    uninspectable = {entry.relative_path for entry in snapshot.entries
        if entry.kind not in {'regular', 'directory'}}
    # A failed listing identifies a directory even at the root, which has no
    # separate directory entry. Its REVIEW belongs to directory accounting.
    unreadable_directories = {path for path, entries in inventory.items()
        if any(entry.kind == 'inaccessible' for entry in entries)
        and any(entry.kind == 'directory' or (entry.diagnostic or '').startswith('listing_error:')
            for entry in entries)}

    files = []
    for path in sorted((set(records) | {p for p, entries in inventory.items()
            if any(e.kind not in {'directory', 'system_excluded'} for e in entries)}) - unreadable_directories):
        entries = inventory.get(path, ())
        kinds = {entry.kind for entry in entries}
        kind = ('inaccessible' if 'inaccessible' in kinds else next(iter(kinds))
            if len(kinds) == 1 else 'ambiguous' if kinds else 'not_in_inventory')
        rows = sorted(records.get(path, ()),
            key=lambda row: (row.object_kind, type(row.object_id).__name__, str(row.object_id)))
        row = rows[0] if len(rows) == 1 else None
        reasons = set()
        if not rows:
            reasons.add('uncovered_file')
        elif len(rows) > 1:
            reasons.add('duplicate_plan_source')
        if kind != 'regular':
            reasons.add('plan_source_not_in_active_inventory' if kind in {'not_in_inventory', 'directory', 'system_excluded'}
                else 'source_not_regular:' + kind)
        if row:
            reasons.update(row.blockers)
            if (row.status not in {'READY', 'WARNING'} or row.action not in {'MOVE', 'KEEP', 'QUARANTINE'}
                    or row.object_kind in {'unknown', 'unresolved_subtitle', 'duplicate_quarantine_candidate'}):
                reasons.add('plan_record_unresolved')
            if not relative_locator_safe(path) or not row.target_relative_path or not relative_locator_safe(row.target_relative_path):
                reasons.add('target_or_source_unresolved')
            if row.action == 'KEEP' and (row.target_relative_path != path or not row.authority):
                reasons.add('keep_not_justified')
            if row.action in {'MOVE', 'QUARANTINE'} and row.target_relative_path == path:
                reasons.add('move_without_relocation')
        file_issues.update(CoverageIssue(path, reason) for reason in reasons)
        files.append(FileCoverage(path, kind, len(rows), row.action if row and not reasons else 'REVIEW',
            row.target_relative_path if row else None, row.object_kind if row else None,
            row.object_id if row else None, tuple((r.object_kind, r.object_id) for r in rows), tuple(sorted(reasons))))

    target_parents, keep_parents = set(), set()
    source_counts, departure_counts = Counter(), Counter()
    for row in plan.records:
        if row.target_relative_path and relative_locator_safe(row.target_relative_path):
            target_parents.update(_parents(row.target_relative_path))
    for item in files:
        for parent in _parents(item.path):
            source_counts[parent] += 1
            departure_counts[parent] += item.disposition in {'MOVE', 'QUARANTINE'}
        if item.disposition == 'KEEP':
            keep_parents.update(_parents(item.path))
    for collision in plan.collisions:
        file_issues.update(CoverageIssue(path, 'target_collision:' + collision.guard) for path in collision.target_paths)

    existing = {path for path, entries in inventory.items()
        if any(entry.kind in {'directory', 'inaccessible', 'system_excluded'} for entry in entries)}
    directories = []
    for path in sorted(existing | target_parents):
        entries = inventory.get(path, ())
        kinds = {entry.kind for entry in entries}
        required = path in target_parents
        uninspectable_ancestor = any(parent in uninspectable for parent in (*_parents(path), '.'))
        subtree_unknown = path in uncertain or uninspectable_ancestor
        empty = None if path in excluded or subtree_unknown or not entries else path not in nonempty_directories
        reasons = []
        if path in excluded:
            disposition, empty = 'EXCLUDED', None
            reasons.append('system_excluded_contents_not_inventoried')
            if required:
                issues.add(CoverageIssue(path, 'target_in_system_excluded_area'))
        elif subtree_unknown:
            disposition = 'REVIEW'
            reasons.append('subtree_not_fully_inventoried')
        elif entries and kinds != {'directory'}:
            disposition, empty = 'REVIEW', None
            reasons.append('target_parent_not_directory')
        elif not entries:
            disposition = 'MISSING_TARGET_PARENT'
            reasons.append('planned_parent_requires_creation')
        elif path in excluded_parents:
            disposition = 'PRESERVED'
            reasons.append('contains_system_excluded_area')
        elif path in keep_parents:
            disposition = 'PRESERVED'
            reasons.append('contains_justified_keep')
        elif required:
            disposition = 'TARGET_REQUIRED'
            reasons.append('parent_of_planned_target')
        elif empty:
            disposition = 'UNRESOLVED_EMPTY'
            reasons.append('empty_directory_disposition_unresolved')
        elif source_counts[path] and source_counts[path] == departure_counts[path]:
            disposition = 'CLEANUP_REVIEW'
            reasons.append('all_known_files_planned_to_leave_cleanup_not_authorized')
        else:
            disposition = 'REVIEW'
            reasons.append('directory_disposition_unresolved')
        if disposition in {'REVIEW', 'UNRESOLVED_EMPTY', 'CLEANUP_REVIEW'}:
            code = 'cleanup_review_required' if disposition == 'CLEANUP_REVIEW' else reasons[0]
            issues.add(CoverageIssue(path, code))
        exists = None if kinds == {'inaccessible'} or (not entries and uninspectable_ancestor) else bool(entries)
        directories.append(DirectoryCoverage(path, disposition, exists, empty,
            required, kinds == {'directory'} and not subtree_unknown, tuple(reasons)))

    issues.update(inventory_issues | file_issues)
    counts = Counter({action: 0 for action in ('MOVE', 'KEEP', 'QUARANTINE', 'REVIEW')})
    counts.update(item.disposition for item in files)
    counts.update({
        'active_regular_files': sum(any(e.kind == 'regular' for e in entries) for entries in inventory.values()),
        'file_entries': len(files), 'plan_records': len(plan.records),
        'existing_directories': sum('directory' in {e.kind for e in entries} for entries in inventory.values()),
        'excluded_areas': len(excluded),
    })
    counts.update('directory_' + item.disposition for item in directories)
    return LibraryCoverage(snapshot.root, plan.plan_hash, plan.filesystem_hash,
        not inventory_issues, not (inventory_issues or file_issues), not issues,
        tuple(sorted(counts.items())), tuple(files), tuple(directories), tuple(sorted(issues)))
