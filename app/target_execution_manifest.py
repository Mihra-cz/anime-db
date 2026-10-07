"""Immutable V6 execution proposal and read-only preflight. No executor lives here."""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import PurePosixPath
from pathlib import Path, PureWindowsPath
import re
import unicodedata
from types import SimpleNamespace
import sqlite3
from typing import get_args, get_origin, get_type_hints
import types

from .target_planner import project_container_locators, relative_locator_safe
from .target_planner_filesystem import plan_library, validate_inventory_root
from .target_planner_post_state import simulate_post_state, verify_post_state
from .target_planner_types import (
    DuplicateExecutionEvidence, FilesystemSnapshot, PlannerContext, SideAssetEvidence, TargetPlan,
)
from .physical_naming_components import evaluate_windows_path_budget
from .tools.target_plan import database_fingerprint

VERSION = 1
POLICY_ID = 'v6-execution-preflight-1'
PLANNER_ID = 'target-planner-post-state-1'
_ACTIONS = frozenset({'KEEP', 'MOVE', 'QUARANTINE'})
_CRITICALITY = frozenset({'PRIMARY', 'LOW', 'REVIEW'})
_REQUIREMENTS = (
    'scanner_paused_through_verification', 'inventory_writer_paused_through_verification',
    'snapshot_bound_to_manifest', 'db_backup_bound_to_manifest',
    'actual_windows_root_checked', 'mount_read_write_checked',
    'safe_write_capability_proven', 'rename_noreplace_capability_proven',
)


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)


def _digest(value) -> str:
    return hashlib.sha256(_canonical(value).encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class DatabaseFingerprint:
    sha256: str
    size: int
    mtime_ns: int
    user_version: int


@dataclass(frozen=True)
class ManifestAction:
    action_id: str
    object_kind: str
    object_id: int | str
    action: str
    source: str
    target: str
    expected_size: int
    expected_mtime_ns: int
    expected_source_kind: str
    expected_target_absent: bool
    criticality: str
    criticality_reason: str
    warning_ids: tuple[str, ...]
    depends_on: tuple[str, ...]
    failure_policy: str

    def __post_init__(self):
        if type(self.warning_ids) is not tuple or type(self.depends_on) is not tuple:
            raise TypeError('manifest action nested values must be tuples')


@dataclass(frozen=True)
class CriticalityAuthority:
    object_kind: str
    object_id: int | str
    severity: str
    reason: str
    actor: str


@dataclass(frozen=True)
class LocatorPatch:
    object_kind: str
    object_id: int
    field: str
    before: str
    after: str


@dataclass(frozen=True)
class WarningRecord:
    warning_id: str
    warning_class: str
    object_kind: str
    object_id: int | str
    source: str


@dataclass(frozen=True)
class WarningAcknowledgement:
    warning_id: str
    warning_class: str
    object_kind: str
    object_id: int | str
    actor: str
    acknowledged_at: str


@dataclass(frozen=True)
class ManifestSideAsset:
    source: str
    secondary_video_id: int
    provenance: str
    expected_size: int | None
    expected_sha256: str | None
    object_kind: str
    object_id: int | str | None
    primary_archive_source: str | None
    primary_archive_sha256: str | None


@dataclass(frozen=True)
class ManifestDuplicateEvidence:
    secondary_video_id: int
    primary_video_id: int
    validity: str
    known_side_assets: tuple[ManifestSideAsset, ...]
    side_asset_accounting: str
    provenance: tuple[str, ...]

    def __post_init__(self):
        if (type(self.known_side_assets) is not tuple or type(self.provenance) is not tuple
                or not all(type(side) is ManifestSideAsset for side in self.known_side_assets)):
            raise TypeError('duplicate evidence nested values must be immutable')


@dataclass(frozen=True)
class GraphReport:
    source_target_chains: int
    cycles: int
    swaps: int
    occupied_targets: int
    case_only: int
    unicode_only: int
    ancestor_interactions: int


@dataclass(frozen=True)
class ManifestPayload:
    version: int
    policy_id: str
    planner_id: str
    plan_hash: str
    db_fingerprint: DatabaseFingerprint
    filesystem_hash: str
    library_root: str
    mount_identity: str
    actions: tuple[ManifestAction, ...]
    directories: tuple[str, ...]
    graph: GraphReport
    locator_patches: tuple[LocatorPatch, ...]
    warnings: tuple[WarningRecord, ...]
    criticality_authorities: tuple[CriticalityAuthority, ...]
    duplicate_evidence: tuple[ManifestDuplicateEvidence, ...]
    requirements: tuple[str, ...]
    counts: tuple[tuple[str, int], ...]
    logical_move_bytes: int
    expected_post_hash: str | None
    generation_diagnostics: tuple[str, ...]

    def __post_init__(self):
        if type(self.db_fingerprint) is not DatabaseFingerprint or type(self.graph) is not GraphReport:
            raise TypeError('manifest payload has mutable or invalid nested model')
        constraints = (
            (self.actions, ManifestAction), (self.directories, str),
            (self.locator_patches, LocatorPatch), (self.warnings, WarningRecord),
            (self.criticality_authorities, CriticalityAuthority),
            (self.duplicate_evidence, ManifestDuplicateEvidence),
            (self.requirements, str), (self.generation_diagnostics, str),
        )
        if any(type(items) is not tuple or not all(type(item) is item_type for item in items)
               for items, item_type in constraints):
            raise TypeError('manifest payload nested values must be immutable tuples')
        if type(self.counts) is not tuple or not all(
                type(item) is tuple and len(item) == 2 and type(item[0]) is str and type(item[1]) is int
                for item in self.counts):
            raise TypeError('manifest counts must be immutable tuples')


@dataclass(frozen=True)
class ExecutionManifest:
    payload: ManifestPayload
    manifest_id: str
    created_at: str
    acknowledgements: tuple[WarningAcknowledgement, ...] = ()

    def __post_init__(self):
        if (type(self.payload) is not ManifestPayload or type(self.acknowledgements) is not tuple
                or not all(type(item) is WarningAcknowledgement for item in self.acknowledgements)):
            raise TypeError('manifest envelope nested values must be immutable')


@dataclass(frozen=True)
class RuntimeEvidence:
    """External assertions for a future operator; this module never performs probes."""
    scanner_paused_through_verification: bool = False
    inventory_writer_paused_through_verification: bool = False
    snapshot_id: str | None = None
    snapshot_at: str | None = None
    snapshot_manifest_id: str | None = None
    snapshot_capable: bool = False
    db_backup_path: str | None = None
    db_backup_sha256: str | None = None
    db_backup_size: int | None = None
    db_backup_user_version: int | None = None
    db_backup_manifest_id: str | None = None
    actual_windows_root: str | None = None
    mount_read_write_checked: bool = False
    mount_read_only_now: bool | None = None
    safe_write_capability_proven: bool = False
    rename_noreplace_capability_proven: bool = False
    mount_identity: str | None = None
    free_bytes: int | None = None


@dataclass(frozen=True)
class PreflightResult:
    outcome: str
    diagnostics: tuple[str, ...]
    checks: tuple[tuple[str, str], ...]
    manifest_id: str
    plan_hash: str | None
    filesystem_hash: str | None
    logical_move_bytes: int


@dataclass(frozen=True)
class JournalEntry:
    """Data contract only: no transition or write function is exposed."""
    manifest_id: str
    action_id: str
    action_index: int
    state: str
    failure_code: str | None = None
    source_intact: bool | None = None
    target_absent: bool | None = None
    primary_dependencies_clear: bool | None = None
    failure_journaled: bool | None = None

    def __post_init__(self):
        if self.state not in {'PENDING', 'FS_APPLIED', 'FS_VERIFIED', 'DB_RECONCILED', 'COMPLETE', 'FAILED'}:
            raise ValueError('unknown journal state')
        if self.action_index < 0 or not self.action_id or not self.manifest_id:
            raise ValueError('invalid journal identity')
        if self.state == 'FAILED' and not self.failure_code:
            raise ValueError('failed journal entry needs failure code')


def journal_to_json(entry: JournalEntry) -> str:
    return _canonical(asdict(entry))


def journal_from_json(raw: str) -> JournalEntry:
    return _decode_type(JournalEntry, _strict_json_load(raw))


def can_continue_after_failure(manifest: ExecutionManifest, entry: JournalEntry) -> bool:
    """Safety contract only; the future executor owns transitions and persistence."""
    _validate_manifest(manifest)
    if entry.manifest_id != manifest.manifest_id or entry.action_index >= len(manifest.payload.actions):
        return False
    action = manifest.payload.actions[entry.action_index]
    if entry.action_id != action.action_id or entry.state != 'FAILED' or action.criticality != 'LOW':
        return False
    if not (entry.failure_code and entry.source_intact and entry.target_absent
            and entry.primary_dependencies_clear and entry.failure_journaled):
        return False
    return not any(action.action_id in other.depends_on and other.criticality == 'PRIMARY'
                   for other in manifest.payload.actions)


def _iso(value: str) -> str:
    if not isinstance(value, str) or not value.endswith('Z'):
        raise ValueError('UTC timestamp required')
    datetime.fromisoformat(value[:-1] + '+00:00')
    return value


def _base_criticality(record, context: PlannerContext, decisions: dict,
                      videos_by_id: dict | None = None, cache: dict | None = None) -> tuple[str, str]:
    if videos_by_id is None:
        videos_by_id = {video.id: video for video in context.videos}
    if cache is None:
        cache = {}
    if record.object_kind == 'video':
        video = videos_by_id.get(record.object_id)
        if video is None:
            return 'REVIEW', 'video_authority_unavailable'
        kind = video.content_type.lower()
        if kind in {'episode', 'film', 'ova', 'special', 'recap'}:
            return 'PRIMARY', 'effective_content_type:' + kind
        if kind in {'op', 'ed', 'ncop', 'nced', 'cm', 'menu', 'pv'}:
            return 'LOW', 'effective_content_type:' + kind
        if kind == 'preview':
            title = context.layout.titles.get(video.title_id)
            if title and title.interpretation == 'story_preview':
                return 'PRIMARY', 'authoritative_story_preview'
            if re.search(r'(?<![A-Za-z0-9])PV\s*0*\d+\b', video.source_evidence_filename or '', re.IGNORECASE):
                return 'LOW', 'parser_pv_evidence'
            return 'REVIEW', 'preview_vs_pv_unresolved'
        if kind == 'bonus':
            naming = context.naming.titles.get(video.title_id)
            if naming and naming.metadata_identity:
                return 'PRIMARY', 'confirmed_own_title_metadata'
            return 'REVIEW', 'bonus_content_authority_unresolved'
        return 'REVIEW', 'content_criticality_unresolved'
    if record.object_kind == 'subtitle':
        severities = []
        for vid, status in record.compatibility:
            if status not in {'automatic_match', 'confirmed_compatible'} or vid not in videos_by_id:
                continue
            if vid not in cache:
                cache[vid] = _criticality(SimpleNamespace(object_kind='video', object_id=vid),
                    context, decisions, videos_by_id, cache)
            severities.append(cache[vid][0])
        if 'PRIMARY' in severities:
            return 'PRIMARY', 'compatible_primary_video'
        if severities and all(s == 'LOW' for s in severities):
            return 'LOW', 'compatible_low_video_only'
        return 'PRIMARY', 'ownerless_subtitle_preservation'
    if record.object_kind in {'confirmed_no_match', 'unresolved_subtitle'}:
        return 'PRIMARY', 'unmatched_subtitle_preservation'
    if record.object_kind in {'auxiliary', 'subtitle_archive', 'unknown', 'duplicate_side_asset', 'duplicate_quarantine_candidate'}:
        return 'PRIMARY', 'unclassified_asset_preservation'
    return 'PRIMARY', 'physical_asset_preservation'


def _criticality(record, context: PlannerContext, decisions: dict,
                 videos_by_id: dict | None = None, cache: dict | None = None) -> tuple[str, str]:
    base = _base_criticality(record, context, decisions, videos_by_id, cache)
    key = (record.object_kind, record.object_id)
    if key not in decisions:
        return base
    decision = decisions[key]
    if (not isinstance(decision, tuple) or len(decision) != 3
            or decision[0] not in {'PRIMARY', 'LOW'} or not isinstance(decision[1], str)
            or not decision[1].strip() or not isinstance(decision[2], str) or not decision[2].strip()):
        raise ValueError('criticality decision requires exact severity, reason and actor')
    if base[0] == 'PRIMARY' and decision[0] == 'LOW':
        raise ValueError('human criticality cannot downgrade mandatory PRIMARY asset')
    return decision[:2]


def criticality_for_records(context: PlannerContext, records, decisions: dict) -> dict:
    videos_by_id = {video.id: video for video in context.videos}
    cache = {}
    result = {}
    for record in records:
        result[(record.object_kind, record.object_id, record.source_relative_path)] = _criticality(
            record, context, decisions, videos_by_id, cache)
    return result


def _patches(context: PlannerContext, plan: TargetPlan) -> tuple[LocatorPatch, ...]:
    baseline = {(b.object_kind, b.object_id, b.field): b.value for b in context.locator_baselines}
    if len(baseline) != len(context.locator_baselines):
        raise ValueError('duplicate locator baseline')
    targets = {}
    for row in plan.records:
        if row.object_kind in {'video', 'subtitle', 'confirmed_no_match', 'unresolved_subtitle'} and row.target_relative_path:
            locator_kind = 'unmatched_subtitle' if row.object_kind in {'confirmed_no_match', 'unresolved_subtitle'} else row.object_kind
            targets[(locator_kind, row.object_id, 'relative_path')] = row.target_relative_path
            if row.object_kind == 'video':
                targets[('video', row.object_id, 'root_folder')] = row.target_relative_path.split('/')[0]
    for locator in project_container_locators(context):
        if locator.status == 'READY' and locator.target_relative_path:
            targets[(locator.object_kind, locator.object_id, 'relative_root_path')] = locator.target_relative_path
    patches = []
    for key, target in sorted(targets.items()):
        if key not in baseline:
            raise ValueError('missing persisted locator baseline: ' + repr(key))
        source = baseline[key]
        if source != target:
            patches.append(LocatorPatch(*key, source, target))
    return tuple(patches)


def _graph_report(actions: tuple[ManifestAction, ...], snapshot: FilesystemSnapshot) -> GraphReport:
    moving = {a.source: a.target for a in actions if a.action != 'KEEP'}
    occupied = {e.relative_path for e in snapshot.entries if e.kind not in {'system_excluded', 'inaccessible'}}
    chains = {source: target for source, target in moving.items() if target in moving}
    cycles = 0
    swaps = 0
    seen = set()
    for start in chains:
        if start in seen:
            continue
        path = []
        positions = {}
        node = start
        while node in chains and node not in seen and node not in positions:
            positions[node] = len(path)
            path.append(node)
            node = chains[node]
        if node in positions:
            cycles += 1
            swaps += int(len(path) - positions[node] == 2)
        seen.update(path)
    source_parents = {str(parent) for path in moving for parent in PurePosixPath(path).parents if str(parent) != '.'}
    target_parents = {str(parent) for path in moving.values() for parent in PurePosixPath(path).parents if str(parent) != '.'}
    return GraphReport(len(chains), cycles, swaps,
        sum(target in occupied for target in moving.values()),
        sum(source != target and source.casefold() == target.casefold() for source, target in moving.items()),
        sum(source != target and unicodedata.normalize('NFC', source) == unicodedata.normalize('NFC', target)
            for source, target in moving.items()),
        sum(source in target_parents or target in source_parents for source, target in moving.items()))


def build_execution_manifest(
    context: PlannerContext, snapshot: FilesystemSnapshot, plan: TargetPlan,
    db_fingerprint: dict, *, created_at: str | None = None,
    mount_identity: str = 'UNVERIFIED',
    acknowledgements: tuple[WarningAcknowledgement, ...] = (),
    criticality_decisions: dict | None = None,
    windows_root: str | None = None,
) -> ExecutionManifest:
    """Seal exactly one approved plan. Never replace it during a later preflight."""
    if plan_library(context, snapshot, windows_root=windows_root) != plan:
        raise ValueError('stale or mismatched plan')
    if plan.filesystem_hash is None:
        raise ValueError('filesystem snapshot is required')
    db = DatabaseFingerprint(**db_fingerprint)
    decisions = criticality_decisions or {}
    authorities = tuple(sorted((CriticalityAuthority(kind, object_id, severity, reason, actor)
        for (kind, object_id), (severity, reason, actor) in decisions.items()),
        key=lambda item: (item.object_kind, str(item.object_id))))
    inventory = {e.relative_path: e for e in snapshot.entries}
    criticality = criticality_for_records(context, plan.records, decisions)
    actions = []
    warnings = []
    diagnostics = []
    for row in plan.records:
        if row.status == 'SYSTEM_EXCLUDED':
            continue
        for warning in row.warnings:
            identity = (warning, row.object_kind, row.object_id, row.source_relative_path)
            warnings.append(WarningRecord(_digest(identity), *identity))
        if row.action not in _ACTIONS or not row.target_relative_path or row.status in {'BLOCKED', 'REVIEW'}:
            diagnostics.append(f'plan_record_not_actionable:{row.object_kind}:{row.object_id}')
            continue
        entry = inventory.get(row.source_relative_path)
        if not entry or entry.kind != 'regular' or entry.size is None or entry.mtime_ns is None:
            diagnostics.append(f'source_not_regular:{row.object_kind}:{row.object_id}')
            continue
        severity, reason = criticality[(row.object_kind, row.object_id, row.source_relative_path)]
        action_id = _digest((row.object_kind, row.object_id, row.action, row.source_relative_path, row.target_relative_path))
        warning_ids = tuple(sorted(_digest((warning, row.object_kind, row.object_id, row.source_relative_path))
            for warning in row.warnings))
        actions.append(ManifestAction(action_id, row.object_kind, row.object_id, row.action,
            row.source_relative_path, row.target_relative_path, entry.size, entry.mtime_ns,
            'regular', row.action != 'KEEP', severity, reason, warning_ids, (),
            'STOP' if severity == 'PRIMARY' else 'CONTINUE_IF_SAFE' if severity == 'LOW' else 'REVIEW'))
    actions.sort(key=lambda a: (a.source, a.object_kind, str(a.object_id)))
    directories = {str(p) for a in actions for p in PurePosixPath(a.target).parents if str(p) != '.'}
    directories = tuple(sorted(directories, key=lambda p: (len(PurePosixPath(p).parts), p)))
    evidence = ()
    expected_post_hash = None
    if not diagnostics and not plan.collisions:
        try:
            projected = simulate_post_state(context, snapshot, plan, windows_root=windows_root)
            expected_post_hash = _semantic_post_hash(projected.context, projected.snapshot,
                projected.execution_evidence, windows_root)
            validity = {r.object_id: r.duplicate.validity for r in plan.records if r.object_kind == 'video' and r.duplicate}
            evidence = tuple(ManifestDuplicateEvidence(e.secondary_video_id, e.primary_video_id,
                validity[e.secondary_video_id], tuple(ManifestSideAsset(**asdict(s)) for s in e.known_side_assets),
                e.side_asset_accounting.value, e.provenance) for e in projected.execution_evidence)
            if not projected.converged:
                diagnostics.append('post_state_not_converged')
        except ValueError as exc:
            diagnostics.append('post_state_unavailable:' + str(exc))
    payload = ManifestPayload(VERSION, POLICY_ID, PLANNER_ID, plan.plan_hash, db,
        plan.filesystem_hash, snapshot.root, mount_identity, tuple(actions), directories,
        _graph_report(tuple(actions), snapshot),
        _patches(context, plan), tuple(sorted(warnings, key=lambda w: w.warning_id)), authorities,
        evidence, _REQUIREMENTS, plan.counts,
        sum(a.expected_size for a in actions if a.action != 'KEEP'),
        expected_post_hash, tuple(sorted(diagnostics)))
    manifest_id = _digest(asdict(payload))
    manifest = ExecutionManifest(payload, manifest_id,
        _iso(created_at or datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')),
        tuple(acknowledgements))
    _validate_manifest(manifest)
    return manifest


def manifest_to_json(manifest: ExecutionManifest) -> str:
    _validate_manifest(manifest)
    return _canonical(asdict(manifest))


def _validate_manifest(manifest: ExecutionManifest) -> None:
    p = manifest.payload
    if p.version != VERSION or p.policy_id != POLICY_ID or p.planner_id != PLANNER_ID:
        raise ValueError('unknown manifest version or policy')
    if not Path(p.library_root).is_absolute():
        raise ValueError('library root must be absolute')
    validate_inventory_root(Path(p.library_root))
    if _digest(asdict(p)) != manifest.manifest_id:
        raise ValueError('manifest hash mismatch')
    _iso(manifest.created_at)
    if p.requirements != _REQUIREMENTS or not p.filesystem_hash or not p.plan_hash:
        raise ValueError('invalid planner requirements or hashes')
    if p.logical_move_bytes != sum(a.expected_size for a in p.actions if a.action != 'KEEP'):
        raise ValueError('move byte count mismatch')
    if any(value < 0 for value in asdict(p.graph).values()):
        raise ValueError('invalid graph report')
    if tuple(sorted(p.actions, key=lambda a:(a.source, a.object_kind, str(a.object_id)))) != p.actions:
        raise ValueError('actions out of canonical order')
    if len({(a.object_kind, a.object_id, a.source) for a in p.actions}) != len(p.actions):
        raise ValueError('duplicate action')
    action_ids = {a.action_id for a in p.actions}
    if len(action_ids) != len(p.actions):
        raise ValueError('duplicate action identity')
    for a in p.actions:
        if a.action not in _ACTIONS or a.criticality not in _CRITICALITY:
            raise ValueError('invalid action or criticality')
        if not relative_locator_safe(a.source) or not relative_locator_safe(a.target):
            raise ValueError('unsafe action locator')
        if any(part.casefold() == '#recycle' for part in PurePosixPath(a.source).parts + PurePosixPath(a.target).parts):
            raise ValueError('system excluded action')
        if a.expected_size < 0 or a.expected_mtime_ns < 0 or not a.criticality_reason:
            raise ValueError('invalid source precondition')
        if (a.action == 'KEEP') != (a.source == a.target):
            raise ValueError('action and locator disagree')
        if a.action_id != _digest((a.object_kind, a.object_id, a.action, a.source, a.target)):
            raise ValueError('action identity mismatch')
        if a.expected_source_kind != 'regular' or a.expected_target_absent != (a.action != 'KEEP'):
            raise ValueError('invalid action precondition')
        if a.failure_policy != ('STOP' if a.criticality == 'PRIMARY' else 'CONTINUE_IF_SAFE' if a.criticality == 'LOW' else 'REVIEW'):
            raise ValueError('invalid failure policy')
        if any(dependency not in action_ids for dependency in a.depends_on):
            raise ValueError('unknown action dependency')
    expected_dirs = {str(parent) for a in p.actions for parent in PurePosixPath(a.target).parents if str(parent) != '.'}
    if p.directories != tuple(sorted(expected_dirs, key=lambda path:(len(PurePosixPath(path).parts), path))):
        raise ValueError('directory dependency mismatch')
    approved_fields = {
        'video': {'relative_path', 'root_folder'},
        'collection': {'relative_root_path'}, 'title': {'relative_root_path'},
        'subtitle': {'relative_path'}, 'unmatched_subtitle': {'relative_path'},
    }
    if len({(v.object_kind, v.object_id, v.field) for v in p.locator_patches}) != len(p.locator_patches):
        raise ValueError('duplicate locator patch')
    for patch in p.locator_patches:
        if patch.field not in approved_fields.get(patch.object_kind, ()):
            raise ValueError('unapproved locator field')
        historical_root_dot = (patch.object_kind == 'video' and patch.field == 'root_folder'
            and patch.before == '.')
        if (not (relative_locator_safe(patch.before) or historical_root_dot)
                or not relative_locator_safe(patch.after)):
            raise ValueError('unsafe locator patch')
    if len({w.warning_id for w in p.warnings}) != len(p.warnings):
        raise ValueError('duplicate warning')
    for warning in p.warnings:
        if warning.warning_id != _digest((warning.warning_class, warning.object_kind, warning.object_id, warning.source)):
            raise ValueError('warning identity mismatch')
    warning_ids = {w.warning_id for w in p.warnings}
    if any(warning_id not in warning_ids for action in p.actions for warning_id in action.warning_ids):
        raise ValueError('action warning identity mismatch')
    if len({(a.object_kind, a.object_id) for a in p.criticality_authorities}) != len(p.criticality_authorities):
        raise ValueError('duplicate criticality authority')
    for authority in p.criticality_authorities:
        if (authority.severity not in {'PRIMARY', 'LOW'} or not authority.reason.strip()
                or not authority.actor.strip() or not any(
                    action.object_kind == authority.object_kind and action.object_id == authority.object_id
                    and action.criticality == authority.severity and action.criticality_reason == authority.reason
                    for action in p.actions)):
            raise ValueError('invalid criticality authority')
    if len({a.warning_id for a in manifest.acknowledgements}) != len(manifest.acknowledgements):
        raise ValueError('duplicate acknowledgement')
    for ack in manifest.acknowledgements:
        _iso(ack.acknowledged_at)
        if not ack.actor.strip():
            raise ValueError('acknowledgement actor required')
    for evidence in p.duplicate_evidence:
        if evidence.side_asset_accounting not in {'COMPLETE', 'INCOMPLETE', 'UNKNOWN'}:
            raise ValueError('invalid duplicate accounting')
        if evidence.validity != 'valid' or evidence.secondary_video_id == evidence.primary_video_id:
            raise ValueError('invalid duplicate relation')
        if any(s.secondary_video_id != evidence.secondary_video_id or not s.provenance for s in evidence.known_side_assets):
            raise ValueError('invalid duplicate side asset')


def _decode_type(annotation, value):
    origin = get_origin(annotation)
    if origin is tuple:
        if type(value) is not list:
            raise ValueError('expected array')
        args = get_args(annotation)
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_decode_type(args[0], x) for x in value)
        if len(value) != len(args):
            raise ValueError('tuple arity mismatch')
        return tuple(_decode_type(t, x) for t, x in zip(args, value))
    if origin in (types.UnionType,):
        for variant in get_args(annotation):
            try:
                return _decode_type(variant, value)
            except ValueError:
                pass
        raise ValueError('type mismatch')
    if annotation is type(None):
        if value is not None:
            raise ValueError('expected null')
        return None
    if hasattr(annotation, '__dataclass_fields__'):
        if type(value) is not dict or set(value) != {f.name for f in fields(annotation)}:
            raise ValueError('unknown or missing field')
        hints = get_type_hints(annotation)
        return annotation(**{f.name: _decode_type(hints[f.name], value[f.name]) for f in fields(annotation)})
    if type(value) is not annotation:
        raise ValueError('type mismatch')
    return value


def _strict_json_load(raw: str):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs)


def manifest_from_json(raw: str) -> ExecutionManifest:
    value = _strict_json_load(raw)
    manifest = _decode_type(ExecutionManifest, value)
    _validate_manifest(manifest)
    return manifest


def duplicate_execution_evidence(manifest: ExecutionManifest) -> tuple[DuplicateExecutionEvidence, ...]:
    """Rehydrate carried scalar proof after JSON and a fresh read-only DB reload."""
    _validate_manifest(manifest)
    return tuple(DuplicateExecutionEvidence(item.secondary_video_id, item.primary_video_id,
        tuple(SideAssetEvidence(**asdict(side)) for side in item.known_side_assets),
        item.side_asset_accounting, item.provenance) for item in manifest.payload.duplicate_evidence)


def _semantic_post_hash(context: PlannerContext, snapshot: FilesystemSnapshot,
                        evidence: tuple[DuplicateExecutionEvidence, ...],
                        windows_root: str | None = None) -> str:
    # mkdir and rename change parent directory timestamps. They are namespace
    # evidence, not a stable postcondition. File stats and archive hashes remain
    # exact inputs to the ordinary post-state planner.
    normalized = replace(snapshot, entries=tuple(replace(entry, mtime_ns=None)
        if entry.kind == 'directory' else entry for entry in snapshot.entries))
    plan = verify_post_state(context, normalized, execution_evidence=evidence,
        windows_root=windows_root)
    return _digest({
        'plan': asdict(plan),
        'locators': [asdict(item) for item in project_container_locators(context)],
        'execution_evidence': [asdict(item) for item in evidence],
    })


def post_state_hash_from_manifest(manifest: ExecutionManifest, context: PlannerContext,
                                  snapshot: FilesystemSnapshot, *, windows_root: str | None = None) -> str:
    """Compute the comparable hash after fresh DB reload and filesystem inventory."""
    return _semantic_post_hash(context, snapshot, duplicate_execution_evidence(manifest), windows_root)


def _runtime_checks(manifest: ExecutionManifest, runtime: RuntimeEvidence) -> list[str]:
    p = manifest.payload
    missing = []
    if not runtime.scanner_paused_through_verification:
        missing.append('scanner_maintenance_not_proven')
    if not runtime.inventory_writer_paused_through_verification:
        missing.append('inventory_writer_maintenance_not_proven')
    valid_snapshot_time = False
    if runtime.snapshot_at:
        try:
            valid_snapshot_time = (datetime.fromisoformat(_iso(runtime.snapshot_at)[:-1] + '+00:00')
                >= datetime.fromisoformat(_iso(manifest.created_at)[:-1] + '+00:00'))
        except ValueError:
            pass
        if not valid_snapshot_time:
            missing.append('snapshot_time_invalid_or_prebaseline')
    if not (runtime.snapshot_capable and runtime.snapshot_id and valid_snapshot_time
            and runtime.snapshot_manifest_id == manifest.manifest_id):
        missing.append('bound_snapshot_not_proven')
    if not (runtime.db_backup_sha256 == p.db_fingerprint.sha256
            and runtime.db_backup_size == p.db_fingerprint.size
            and runtime.db_backup_user_version == p.db_fingerprint.user_version
            and runtime.db_backup_manifest_id == manifest.manifest_id):
        missing.append('bound_db_backup_not_proven')
    if not runtime.db_backup_path:
        missing.append('db_backup_local_path_not_proven')
    else:
        backup = Path(runtime.db_backup_path).resolve(strict=False)
        repo = Path(__file__).resolve().parent.parent
        library = Path(p.library_root).resolve(strict=False)
        if backup == repo or repo in backup.parents or backup == library or library in backup.parents:
            missing.append('db_backup_not_outside_repo_and_library')
        elif not backup.is_file() or Path(runtime.db_backup_path).is_symlink():
            missing.append('db_backup_file_missing_or_not_regular')
        elif backup.stat().st_nlink != 1:
            missing.append('db_backup_not_independent_copy')
        else:
            try:
                actual_backup = database_fingerprint(backup)
                if any(actual_backup[key] != getattr(p.db_fingerprint, key)
                       for key in ('sha256', 'size', 'user_version')):
                    missing.append('db_backup_bytes_mismatch')
            except (OSError, sqlite3.Error):
                missing.append('db_backup_unreadable')
    if not runtime.actual_windows_root:
        missing.append('actual_windows_root_not_proven')
    else:
        try:
            evaluate_windows_path_budget(runtime.actual_windows_root)
            for action in p.actions:
                absolute = str(PureWindowsPath(runtime.actual_windows_root).joinpath(*PurePosixPath(action.target).parts))
                if not evaluate_windows_path_budget(absolute).within_target:
                    missing.append('windows_path_budget:' + action.target)
        except (ValueError, TypeError):
            missing.append('actual_windows_root_invalid')
    if not runtime.mount_read_write_checked:
        missing.append('mount_read_write_not_proven')
    if runtime.mount_read_only_now is True:
        missing.append('current_mount_read_only')
    elif runtime.mount_read_only_now is None:
        missing.append('current_mount_state_not_proven')
    if not runtime.safe_write_capability_proven:
        missing.append('safe_write_capability_not_proven')
    if not runtime.rename_noreplace_capability_proven:
        missing.append('rename_noreplace_not_proven')
    if p.mount_identity == 'UNVERIFIED' or runtime.mount_identity != p.mount_identity:
        missing.append('mount_identity_not_proven')
    if runtime.free_bytes is None:
        missing.append('free_bytes_not_reported')
    return missing


def preflight_execution_manifest(
    manifest: ExecutionManifest, context: PlannerContext, snapshot: FilesystemSnapshot,
    db_fingerprint: dict, *, runtime: RuntimeEvidence | None = None,
    windows_root: str | None = None,
) -> PreflightResult:
    """Compare fresh read models to sealed proposal; never mutate the proposal."""
    diagnostics = []
    checks = []
    p = manifest.payload
    try:
        _validate_manifest(manifest)
    except ValueError as exc:
        return PreflightResult('ERROR', ('invalid_manifest:' + str(exc),), (), manifest.manifest_id, None, None, p.logical_move_bytes)
    if DatabaseFingerprint(**db_fingerprint) != p.db_fingerprint:
        diagnostics.append('database_fingerprint_changed')
    if snapshot.root != p.library_root:
        diagnostics.append('library_root_changed')
    inventory = {e.relative_path: e for e in snapshot.entries}
    symlinks = []
    for action in p.actions:
        for path in (action.source, *(str(parent) for parent in PurePosixPath(action.source).parents if str(parent) != '.')):
            entry = inventory.get(path)
            if entry and entry.kind == 'symlink':
                symlinks.append('source_symlink:' + path)
    if diagnostics:
        checks.append(('database_fingerprint', 'CHANGED'))
        return PreflightResult('STALE', tuple(sorted(set(diagnostics))), (), manifest.manifest_id,
            None, None, p.logical_move_bytes)
    checks.append(('database_fingerprint', 'MATCH'))
    if symlinks:
        # A sealed regular source that became a link is a filesystem change.
        return PreflightResult('STALE', tuple(sorted(set(symlinks))), (), manifest.manifest_id,
            None, None, p.logical_move_bytes)
    for action in p.actions:
        entry = inventory.get(action.source)
        if (not entry or entry.kind != 'regular' or entry.size != action.expected_size
                or entry.mtime_ns != action.expected_mtime_ns):
            diagnostics.append('source_changed:' + action.source)
    if diagnostics:
        return PreflightResult('STALE', tuple(sorted(set(diagnostics))), (), manifest.manifest_id, None, None, p.logical_move_bytes)
    checks.append(('source_preconditions', 'MATCH'))
    try:
        fresh = plan_library(context, snapshot, windows_root=windows_root)
    except Exception as exc:
        return PreflightResult('ERROR', ('planner_error:' + type(exc).__name__,), (), manifest.manifest_id, None, None, p.logical_move_bytes)
    blockers = list(p.generation_diagnostics)
    if p.filesystem_hash != fresh.filesystem_hash:
        checks.append(('filesystem_snapshot', 'CHANGED'))
    else:
        checks.append(('filesystem_snapshot', 'MATCH'))
    source_set = {a.source for a in p.actions if a.action != 'KEEP'}
    occupied = set()
    for action in p.actions:
        if not relative_locator_safe(action.source) or not relative_locator_safe(action.target):
            blockers.append('unsafe_locator:' + action.source)
            continue
        if '#recycle' in (part.casefold() for part in PurePosixPath(action.target).parts):
            blockers.append('system_excluded_target:' + action.target)
        target = inventory.get(action.target)
        if action.action != 'KEEP' and target:
            if target.kind == 'symlink':
                blockers.append('target_symlink:' + action.target)
            elif action.target not in source_set:
                blockers.append('target_occupied:' + action.target)
            else:
                occupied.add(action.target)
        for parent in PurePosixPath(action.target).parents:
            if str(parent) == '.':
                break
            entry = inventory.get(str(parent))
            if entry and entry.kind != 'directory':
                blockers.append('target_parent_not_directory:' + str(parent))
            if not entry and str(parent) not in p.directories:
                blockers.append('unplanned_target_directory:' + str(parent))
    if occupied:
        moves = {a.source: a.target for a in p.actions if a.action != 'KEEP'}
        for source in occupied:
            seen = set()
            node = source
            while node in moves and node not in seen:
                seen.add(node)
                node = moves[node]
            if node in seen:
                blockers.append('move_cycle:' + source)
            else:
                blockers.append('occupied_by_source_requires_staging:' + source)
    checks.append(('target_namespace', 'BLOCKED' if any(x.startswith(
        ('target_occupied:', 'target_symlink:', 'system_excluded_target:', 'occupied_by_source_requires_staging:', 'move_cycle:'))
        for x in blockers) else 'MATCH'))
    checks.append(('directory_dependencies', 'BLOCKED' if any(x.startswith(
        ('target_parent_not_directory:', 'unplanned_target_directory:')) for x in blockers) else 'MATCH'))
    if fresh.plan_hash != p.plan_hash:
        # Any DB/FS/planner difference invalidates the sealed approval. Namespace
        # findings stay visible, but this manifest can never become READY again.
        namespace = {x for x in blockers if x.startswith(
            ('target_occupied:', 'target_symlink:', 'target_parent_not_directory:'))}
        return PreflightResult('STALE', tuple(sorted({'plan_hash_changed', *namespace})), tuple(checks),
            manifest.manifest_id, fresh.plan_hash, fresh.filesystem_hash, p.logical_move_bytes)
    try:
        reference = build_execution_manifest(context, snapshot, fresh, db_fingerprint,
            created_at=manifest.created_at, mount_identity=p.mount_identity,
            criticality_decisions={(a.object_kind, a.object_id): (a.severity, a.reason, a.actor)
                for a in p.criticality_authorities},
            windows_root=windows_root)
    except (ValueError, TypeError) as exc:
        return PreflightResult('ERROR', ('reference_projection_error:' + str(exc),), tuple(checks),
            manifest.manifest_id, fresh.plan_hash, fresh.filesystem_hash, p.logical_move_bytes)
    if reference.payload != p:
        blockers.append('manifest_projection_mismatch')
    checks.append(('manifest_projection', 'MATCH' if reference.payload == p else 'MISMATCH'))
    checks.append(('graph', _canonical(asdict(p.graph))))
    if (p.graph.cycles or p.graph.swaps or p.graph.occupied_targets or p.graph.ancestor_interactions
            or p.graph.case_only or p.graph.unicode_only):
        blockers.append('unresolved_move_graph')
    if any(a.criticality == 'REVIEW' for a in p.actions):
        blockers.append('criticality_review_required')
    if p.expected_post_hash is None:
        blockers.append('expected_post_state_unavailable')
    expected = {w.warning_id: w for w in p.warnings}
    acknowledgements = {a.warning_id: a for a in manifest.acknowledgements}
    for warning in p.warnings:
        ack = acknowledgements.get(warning.warning_id)
        if not ack or (ack.warning_class, ack.object_kind, ack.object_id) != (
                warning.warning_class, warning.object_kind, warning.object_id) or not ack.actor.strip():
            blockers.append('warning_unacknowledged:' + warning.warning_id)
    if set(acknowledgements) - set(expected):
        blockers.append('unknown_warning_acknowledgement')
    checks.append(('warning_acknowledgements', 'BLOCKED' if any(x.startswith(
        ('warning_unacknowledged:', 'unknown_warning_acknowledgement')) for x in blockers) else 'MATCH'))
    runtime_missing = _runtime_checks(manifest, runtime or RuntimeEvidence())
    blockers.extend(runtime_missing)
    checks.append(('runtime_requirements', 'BLOCKED' if runtime_missing else 'MATCH'))
    return PreflightResult('NOT_READY' if blockers else 'READY_FOR_EXECUTION',
        tuple(sorted(set(blockers))), tuple(checks), manifest.manifest_id,
        fresh.plan_hash, fresh.filesystem_hash, p.logical_move_bytes)
