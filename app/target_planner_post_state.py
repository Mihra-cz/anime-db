"""Pure intended-state projection and fixed-point proof; never an executor.

Inputs are DB-known identities and an approved read model. No filesystem access,
SQL, scanner inference, manifest, journal or production locator writes occur.
"""
from __future__ import annotations

from dataclasses import asdict, replace
import hashlib
import json
from pathlib import PurePosixPath

from .target_planner import project_container_locators
from .target_planner_filesystem import plan_library
from .target_planner_types import (
    DuplicateExecutionEvidence, FilesystemEntry, FilesystemSnapshot,
    PlannerContext, PostStateProjection, SideAssetEvidence, TargetPlan,
)


def verify_post_state(
    context: PlannerContext, snapshot: FilesystemSnapshot, *,
    execution_evidence: tuple[DuplicateExecutionEvidence, ...] = (),
    windows_root: str | None = None,
) -> TargetPlan:
    """Verify a normal DB reload with explicit immutable execution facts.

    The future manifest/journal supplies these facts. Omission deliberately
    discards any in-memory carried evidence and leaves accounting UNKNOWN in
    quarantine; physical paths never grant provenance or delete authority.
    """
    return plan_library(replace(context, side_assets=(), execution_evidence=execution_evidence),
                        snapshot, windows_root=windows_root)


def _project_execution_evidence(context, snapshot, plan):
    """Expose carried scalar facts for both pure and real-reload verification."""
    inventory = {e.relative_path: e for e in snapshot.entries}
    planned_by_source = {r.source_relative_path: r for r in plan.records}
    previous = {s.source: s for item in context.execution_evidence for s in item.known_side_assets}
    previous.update({s.source: s for s in context.side_assets})
    videos = {v.id: v for v in context.videos}
    result = []
    for row in plan.records:
        if row.object_kind != 'video' or row.duplicate is None:
            continue
        d = row.duplicate
        old = next((e for e in context.execution_evidence if e.secondary_video_id == row.object_id), None)
        old_paths = {s.source for s in old.known_side_assets} if old else set()
        sides = []
        for other in plan.records:
            if other.object_kind == 'video':
                continue
            source = other.source_relative_path
            owned = other.duplicate and other.duplicate.secondary_video_id == row.object_id
            # Only assets owned by this secondary are its side assets. Directory
            # proximity (primary sidecars, auxiliary, other secondaries) never
            # transfers ownership; INCOMPLETE/UNKNOWN accounting is carried as state.
            if other.duplicate and not owned:
                continue
            if not (owned or source in old_paths):
                continue
            entry = inventory[source]
            prior = previous.get(source)
            archive_source = prior.primary_archive_source if prior else None
            archive_hash = prior.primary_archive_sha256 if prior else None
            if owned and other.object_kind == 'duplicate_side_asset' and prior is None:
                primary = videos[d.primary_video_id]
                archive_source = str(PurePosixPath(primary.source).parent / PurePosixPath(source).name)
                counterpart = inventory.get(archive_source)
                archive_hash = counterpart.content_sha256 if counterpart else None
            if archive_source in planned_by_source:
                archive_source = planned_by_source[archive_source].target_relative_path
            sides.append(SideAssetEvidence(other.target_relative_path, row.object_id,
                prior.provenance if prior else ';'.join(other.authority),
                prior.expected_size if prior else entry.size,
                prior.expected_sha256 if prior else entry.content_sha256,
                other.object_kind, other.object_id if isinstance(other.object_id, int) else other.target_relative_path,
                archive_source, archive_hash))
        result.append(DuplicateExecutionEvidence(row.object_id, d.primary_video_id,
            tuple(sorted(sides, key=lambda s:(s.source, s.object_kind, str(s.object_id)))),
            d.side_asset_accounting, d.accounting_provenance or ('accounting_evidence_unavailable',)))
    return tuple(sorted(result, key=lambda e:e.secondary_video_id))


def simulate_post_state(
    context: PlannerContext, snapshot: FilesystemSnapshot, approved_plan: TargetPlan,
    *, windows_root: str | None = None,
) -> PostStateProjection:
    """Apply only approved locators in memory and rerun the same planner.

    Side-asset provenance is carried from verified plan evidence, not recovered
    from a quarantine pathname. Incomplete accounting remains incomplete even
    when relocation separates the original source-directory neighbours.
    """
    if plan_library(context, snapshot, windows_root=windows_root) != approved_plan:
        raise ValueError('Post-state projection received a stale plan/context/snapshot.')
    if any(r.status in {'BLOCKED', 'REVIEW'} or not r.target_relative_path for r in approved_plan.records):
        raise ValueError('Post-state projection requires a plan without BLOCKED/REVIEW records.')
    by_source = {r.source_relative_path: r for r in approved_plan.records}
    db_sources = (item.source for items in (context.videos, context.subtitles, context.unmatched) for item in items)
    if any(source not in by_source for source in db_sources):
        raise ValueError('A DB-known asset has no approved locator (possibly system-excluded).')
    locators = project_container_locators(context)
    if any(r.status != 'READY' for r in locators):
        raise ValueError('Post-state projection has unresolved container locator REVIEW.')
    collection_targets = {r.object_id: r.target_relative_path for r in locators if r.object_kind == 'collection'}
    title_targets = {r.object_id: r.target_relative_path for r in locators if r.object_kind == 'title'}
    entries = {e.relative_path: e for e in snapshot.entries if e.kind in {'directory', 'system_excluded'}}
    for entry in snapshot.entries:
        if entry.kind in {'directory', 'system_excluded'}:
            continue
        row = by_source.get(entry.relative_path)
        if row is None or not row.target_relative_path:
            raise ValueError('Post-state projection requires complete file coverage.')
        target = row.target_relative_path
        if target in entries:
            raise ValueError('Post-state projection has a target namespace conflict.')
        # Normal inventory does not hash the owner-less Subs preservation lane.
        entries[target] = replace(entry, relative_path=target,
            content_sha256=None if PurePosixPath(target).parts[0] == 'Subs' else entry.content_sha256)
    # Preserve old directories; predicted missing parents carry no fabricated
    # timestamps. These are snapshot entries, never mkdir actions or syscalls.
    for path in tuple(entries):
        for parent in PurePosixPath(path).parents:
            if str(parent) != '.':
                entries.setdefault(str(parent), FilesystemEntry(str(parent), 'directory'))

    evidence = _project_execution_evidence(context, snapshot, approved_plan)
    projected = replace(context,
        collections=tuple(replace(c, locators=(collection_targets[c.id],)) for c in sorted(context.collections, key=lambda c:c.id)),
        titles=tuple(replace(t, source_locator=title_targets[t.id]) for t in sorted(context.titles, key=lambda t:t.id)),
        # Only the current physical locator changes. In particular, retain
        # source_evidence_filename and all resolved domain/parser facts.
        videos=tuple(replace(v, source=by_source[v.source].target_relative_path) for v in sorted(context.videos, key=lambda v:v.id)),
        subtitles=tuple(replace(s, source=by_source[s.source].target_relative_path) for s in sorted(context.subtitles, key=lambda s:s.id)),
        unmatched=tuple(replace(s, source=by_source[s.source].target_relative_path) for s in sorted(context.unmatched, key=lambda s:s.id)),
        side_assets=(), execution_evidence=evidence,
    )
    projected_snapshot = replace(snapshot, entries=tuple(sorted(entries.values(), key=lambda e:e.relative_path)))
    result = verify_post_state(projected, projected_snapshot, execution_evidence=evidence, windows_root=windows_root)
    blockers = []
    if result.collisions:
        blockers.append('post_state_collisions')
    if any(r.status in {'BLOCKED', 'REVIEW'} for r in result.records):
        blockers.append('post_state_blocked_or_review')
    if any(r.action != 'KEEP' or r.source_relative_path != r.target_relative_path for r in result.records):
        blockers.append('post_state_target_drift')
    intended = {(r.object_kind, r.target_relative_path) for r in approved_plan.records}
    actual = {(r.object_kind, r.source_relative_path) for r in result.records}
    if intended != actual:
        blockers.append('post_state_classification_or_coverage_drift')
    rerun_locators = project_container_locators(projected)
    if locators != rerun_locators:
        blockers.append('post_state_container_locator_drift')
    payload = dict(plan_hash=result.plan_hash, locators=[asdict(r) for r in rerun_locators],
        execution_evidence=[asdict(s) for s in evidence], blockers=blockers)
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()
    return PostStateProjection(projected, projected_snapshot, rerun_locators, result, not blockers, tuple(blockers), digest, evidence)
