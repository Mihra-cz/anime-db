"""Execution manifest is a sealed read model; preflight never performs writes."""
from dataclasses import FrozenInstanceError, replace
import json

import pytest

from app.target_planner_filesystem import plan_library
from app.target_planner_types import FilesystemEntry, FilesystemSnapshot, LocatorBaseline
from app.target_execution_manifest import (
    build_execution_manifest, manifest_from_json, manifest_to_json,
    preflight_execution_manifest, WarningAcknowledgement, RuntimeEvidence,
)
from test_target_planner import context


def fixture(*, content='episode', source='old/source.mkv'):
    ctx = context(kind='bonus' if content == 'bonus' else 'season', content=content,
        layout='extras_bonus' if content == 'bonus' else None, ordinal=1 if content == 'bonus' else None)
    ctx = replace(ctx, videos=(replace(ctx.videos[0], source=source),),
        locator_baselines=(
            LocatorBaseline('collection', 1, 'relative_root_path', 'old'),
            LocatorBaseline('title', 10, 'relative_root_path', 'old'),
            LocatorBaseline('video', 100, 'relative_path', source),
            LocatorBaseline('video', 100, 'root_folder', 'old'),
        ))
    snap = FilesystemSnapshot('/library', (FilesystemEntry(source, 'regular', 7, 9),), 255, 4096)
    return ctx, snap


DB = {'sha256': 'a'*64, 'size': 100, 'mtime_ns': 10, 'user_version': 9}


def make(ctx=None, snap=None, **kwargs):
    if ctx is None:
        ctx, snap = fixture()
    return build_execution_manifest(ctx, snap, plan_library(ctx, snap), DB, created_at='2026-10-07T00:00:00Z', **kwargs)


def test_manifest_hash_is_deterministic_and_timestamp_outside_hash():
    ctx, snap = fixture()
    first = make(ctx, snap)
    second = build_execution_manifest(ctx, snap, plan_library(ctx, snap), DB, created_at='2026-10-08T00:00:00Z')
    assert first.manifest_id == second.manifest_id
    assert manifest_from_json(manifest_to_json(first)) == first
    with pytest.raises(FrozenInstanceError):
        first.payload.actions[0].source = 'elsewhere'
    with pytest.raises((TypeError, ValueError)):
        replace(first.payload, actions=list(first.payload.actions))


def test_manifest_locator_patches_never_touch_historical_filename():
    manifest = make()
    assert {(p.object_kind, p.field) for p in manifest.payload.locator_patches} == {
        ('collection', 'relative_root_path'), ('title', 'relative_root_path'),
        ('video', 'relative_path'), ('video', 'root_folder')}
    assert all(p.field != 'filename' for p in manifest.payload.locator_patches)


def test_root_level_video_dot_baseline_is_exact_precondition_not_target_authority():
    ctx, snap = fixture()
    ctx = replace(ctx,
        videos=(replace(ctx.videos[0], source_evidence_filename='Historical original.mkv'),),
        locator_baselines=tuple(replace(b, value='.') if (b.object_kind, b.object_id, b.field)
            == ('video', 100, 'root_folder') else b for b in ctx.locator_baselines))
    manifest = make(ctx, snap)
    patch = next(p for p in manifest.payload.locator_patches if p.object_kind == 'video' and p.field == 'root_folder')
    assert (patch.before, patch.after) == ('.', 'Show')
    loaded = manifest_from_json(manifest_to_json(manifest))
    assert loaded == manifest
    assert preflight_execution_manifest(loaded, ctx, snap, DB).outcome == 'NOT_READY'
    assert ctx.videos[0].source_evidence_filename == 'Historical original.mkv'
    assert all(p.field != 'filename' for p in loaded.payload.locator_patches)
    for unsafe in ('..', '/outside'):
        bad = replace(ctx, locator_baselines=tuple(replace(b, value=unsafe)
            if (b.object_kind, b.object_id, b.field) == ('video', 100, 'root_folder') else b
            for b in ctx.locator_baselines))
        with pytest.raises(ValueError, match='unsafe locator patch'):
            make(bad, snap)


def test_preflight_detects_db_and_source_drift_as_stale():
    ctx, snap = fixture()
    manifest = make(ctx, snap)
    changed_db = preflight_execution_manifest(manifest, ctx, snap, {**DB, 'sha256': 'b'*64})
    assert changed_db.outcome == 'STALE'
    changed_snap = replace(snap, entries=(replace(snap.entries[0], mtime_ns=10),))
    changed_source = preflight_execution_manifest(manifest, ctx, changed_snap, DB)
    assert changed_source.outcome == 'STALE'
    assert changed_source.diagnostics == ('source_changed:old/source.mkv',)


def test_unproved_bonus_criticality_is_review_not_low():
    ctx, snap = fixture(content='bonus')
    names = dict(ctx.naming.titles)
    names[10] = replace(names[10], metadata_identity=None)
    from app.physical_naming import NamingChoice, create_basis_snapshot
    from test_target_planner import NOW
    naming = replace(ctx.naming, titles=names)
    choices = dict(naming.choices)
    choices[('title', 10)] = NamingChoice(2, 'Show', 'custom', NOW, create_basis_snapshot(naming, 'title', 10))
    ctx = replace(ctx, naming=replace(naming, choices=choices))
    manifest = make(ctx, snap)
    assert manifest.payload.actions[0].criticality == 'REVIEW'
    assert preflight_execution_manifest(manifest, ctx, snap, DB).outcome == 'NOT_READY'


def test_strict_json_rejects_tampering_and_unknown_fields():
    encoded = json.loads(manifest_to_json(make()))
    encoded['payload']['actions'][0]['source'] = 'changed'
    with pytest.raises(ValueError):
        manifest_from_json(json.dumps(encoded))


def test_recomputed_manifest_cannot_name_recycle_as_library_root():
    import hashlib
    encoded = json.loads(manifest_to_json(make()))
    encoded['payload']['library_root'] = '/tmp/#recycle'
    encoded['manifest_id'] = hashlib.sha256(json.dumps(encoded['payload'], sort_keys=True,
        separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()
    with pytest.raises(ValueError, match='library root'):
        manifest_from_json(json.dumps(encoded))


def test_cli_rejects_recycle_root_before_database_load(tmp_path, monkeypatch):
    import app.tools.execution_manifest as cli
    recycle = tmp_path / '#recycle'
    recycle.mkdir()
    monkeypatch.setattr(cli, '_read_current', lambda *args, **kwargs: pytest.fail('database loaded'))
    with pytest.raises(ValueError, match='library root'):
        cli.main(['generate-manifest', '--db', str(tmp_path/'missing.db'),
            '--library-root', str(recycle), '--output', str(tmp_path/'manifest.json')])


def test_confirmed_no_match_gets_exact_unresolved_table_locator_patch():
    ctx, snap = fixture()
    from app.target_planner_types import UnmatchedSubtitle
    ctx = replace(ctx, unmatched=(UnmatchedSubtitle(5, 'old/orphan.ass', 'confirmed_no_match'),),
        locator_baselines=ctx.locator_baselines + (LocatorBaseline('unmatched_subtitle', 5, 'relative_path', 'old/orphan.ass'),))
    snap = replace(snap, entries=snap.entries + (FilesystemEntry('old/orphan.ass', 'regular', 3, 4),))
    manifest = make(ctx, snap)
    assert any(p.object_kind == 'unmatched_subtitle' and p.object_id == 5 and p.field == 'relative_path'
               for p in manifest.payload.locator_patches)
    assert next(a for a in manifest.payload.actions if a.object_id == 5).criticality == 'PRIMARY'


def test_new_target_occupant_or_symlink_is_stale_with_specific_diagnostic():
    # Any filesystem change after sealing invalidates the approval (STALE); the
    # namespace reason stays visible but never suggests the same manifest can pass.
    ctx, snap = fixture()
    manifest = make(ctx, snap)
    target = manifest.payload.actions[0].target
    for kind, code in [('regular', 'target_occupied'), ('symlink', 'target_symlink')]:
        changed = replace(snap, entries=snap.entries + (FilesystemEntry(target, kind, 1, 1),))
        result = preflight_execution_manifest(manifest, ctx, changed, DB)
        assert result.outcome == 'STALE'
        assert 'plan_hash_changed' in result.diagnostics
        assert any(x.startswith(code) for x in result.diagnostics)


def test_recomputed_hash_cannot_authorize_delete_or_unsafe_path():
    from dataclasses import asdict
    import hashlib
    encoded = json.loads(manifest_to_json(make()))
    encoded['payload']['actions'][0]['action'] = 'DELETE'
    encoded['manifest_id'] = hashlib.sha256(json.dumps(encoded['payload'], sort_keys=True,
        separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()
    with pytest.raises(ValueError):
        manifest_from_json(json.dumps(encoded))
    encoded = json.loads(manifest_to_json(make()))
    encoded['payload']['actions'][0]['target'] = '../outside.mkv'
    encoded['manifest_id'] = hashlib.sha256(json.dumps(encoded['payload'], sort_keys=True,
        separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()
    with pytest.raises(ValueError):
        manifest_from_json(json.dumps(encoded))


def test_exact_warning_acknowledgement_required():
    ctx, snap = fixture()
    from app.target_planner_types import UnmatchedSubtitle
    ctx = replace(ctx, unmatched=(UnmatchedSubtitle(5, 'old/orphan.ass', 'confirmed_no_match'),),
        locator_baselines=ctx.locator_baselines + (LocatorBaseline('unmatched_subtitle', 5, 'relative_path', 'old/orphan.ass'),))
    snap = replace(snap, entries=snap.entries + (FilesystemEntry('old/orphan.ass', 'regular', 3, 4),))
    manifest = make(ctx, snap)
    warning = manifest.payload.warnings[0]
    assert any(d.startswith('warning_unacknowledged:') for d in preflight_execution_manifest(manifest, ctx, snap, DB).diagnostics)
    ack = WarningAcknowledgement(warning.warning_id, warning.warning_class,
        warning.object_kind, warning.object_id, 'operator', '2026-10-07T01:00:00Z')
    acknowledged = replace(manifest, acknowledgements=(ack,))
    assert not any(d.startswith('warning_unacknowledged:') for d in preflight_execution_manifest(acknowledged, ctx, snap, DB).diagnostics)
    added = replace(ctx, unmatched=ctx.unmatched + (UnmatchedSubtitle(6, 'old/new.ass', 'confirmed_no_match'),),
        locator_baselines=ctx.locator_baselines + (LocatorBaseline('unmatched_subtitle', 6, 'relative_path', 'old/new.ass'),))
    changed_snap = replace(snap, entries=snap.entries + (FilesystemEntry('old/new.ass', 'regular', 4, 5),))
    assert preflight_execution_manifest(acknowledged, added, changed_snap, DB).outcome == 'STALE'


def test_manifest_output_rejects_library_and_alias(tmp_path):
    from app.tools.execution_manifest import validate_local_output
    library = tmp_path / 'library'
    library.mkdir()
    (tmp_path / 'alias').symlink_to(library, target_is_directory=True)
    with pytest.raises(ValueError):
        validate_local_output(library / 'manifest.json', library)
    with pytest.raises(ValueError):
        validate_local_output(tmp_path / 'alias' / 'manifest.json', library)
    assert validate_local_output(tmp_path / 'local.json', library) == tmp_path / 'local.json'
    second = tmp_path / 'other_library'
    second.mkdir()
    with pytest.raises(ValueError):
        validate_local_output(second / 'dryrun.json', library, second)


def test_manifest_output_rejects_network_mount(tmp_path, monkeypatch):
    import app.tools.execution_manifest as cli
    monkeypatch.setattr(cli, '_mount_fstype', lambda path: 'cifs')
    with pytest.raises(ValueError):
        cli.validate_local_output(tmp_path / 'manifest.json', tmp_path / 'unrelated_library')


def test_manifest_output_cannot_replace_database_or_input_manifest(tmp_path):
    from app.tools.execution_manifest import validate_local_output
    db = tmp_path / 'test.db'
    db.write_bytes(b'production sentinel')
    hardlink = tmp_path / 'db-hardlink'
    hardlink.hardlink_to(db)
    before = db.read_bytes()
    for output in (db, hardlink):
        with pytest.raises(ValueError):
            validate_local_output(output, tmp_path / 'library', forbidden_files=(db,))
    assert db.read_bytes() == before
    manifest = tmp_path / 'input-manifest.json'
    manifest.write_text('{"sentinel": true}')
    with pytest.raises(ValueError):
        validate_local_output(manifest, tmp_path / 'library', forbidden_files=(db, manifest))
    assert manifest.read_text() == '{"sentinel": true}'


def test_projected_context_carries_post_locator_baselines_for_fresh_reload_parity():
    from app.target_planner_post_state import simulate_post_state
    ctx, snap = fixture()
    projection = simulate_post_state(ctx, snap, plan_library(ctx, snap))
    values = {(b.object_kind, b.object_id, b.field): b.value for b in projection.context.locator_baselines}
    assert values[('video', 100, 'relative_path')] == 'Show/Season 01/Show - S01E01.mkv'
    assert values[('video', 100, 'root_folder')] == 'Show'
    assert values[('collection', 1, 'relative_root_path')] == 'Show'


def test_expected_post_hash_ignores_directory_mtime_but_catches_file_drift():
    from app.target_execution_manifest import post_state_hash_from_manifest
    from app.target_planner_post_state import simulate_post_state
    ctx, snap = fixture()
    plan = plan_library(ctx, snap)
    manifest = make(ctx, snap)
    projected = simulate_post_state(ctx, snap, plan)
    assert manifest.payload.expected_post_hash == post_state_hash_from_manifest(
        manifest, projected.context, projected.snapshot)
    directory_time_changed = replace(projected.snapshot, entries=tuple(
        replace(e, mtime_ns=999) if e.kind == 'directory' else e
        for e in projected.snapshot.entries))
    assert post_state_hash_from_manifest(manifest, projected.context, directory_time_changed) == manifest.payload.expected_post_hash
    file_time_changed = replace(projected.snapshot, entries=tuple(
        replace(e, mtime_ns=999) if e.kind == 'regular' else e
        for e in projected.snapshot.entries))
    assert post_state_hash_from_manifest(manifest, projected.context, file_time_changed) != manifest.payload.expected_post_hash


@pytest.mark.parametrize('content,expected', [
    ('episode', 'PRIMARY'), ('film', 'PRIMARY'), ('ova', 'PRIMARY'),
    ('special', 'PRIMARY'), ('recap', 'PRIMARY'),
    ('op', 'LOW'), ('ed', 'LOW'), ('ncop', 'LOW'), ('nced', 'LOW'),
    ('cm', 'LOW'), ('pv', 'LOW'), ('menu', 'LOW'),
])
def test_video_criticality_approved_type_matrix(content, expected):
    from app.target_execution_manifest import _criticality
    from app.target_planner_types import PlanRecord
    ctx, _ = fixture()
    ctx = replace(ctx, videos=(replace(ctx.videos[0], content_type=content),))
    record = PlanRecord('video', 100, 'old/source.mkv', 'Show/a.mkv', 'MOVE', 'READY')
    assert _criticality(record, ctx, {})[0] == expected


def test_subtitle_criticality_is_max_compatible_video_and_d06_primary():
    from app.target_execution_manifest import _criticality
    from app.target_planner_types import PlanRecord
    ctx, _ = fixture()
    videos = (replace(ctx.videos[0], content_type='op'),
        replace(ctx.videos[0], id=101, content_type='episode'))
    ctx = replace(ctx, videos=videos)
    subtitle = PlanRecord('subtitle', 8, 'old/sub.ass', 'Show/sub.ass', 'MOVE', 'READY',
        compatibility=((100, 'automatic_match'), (101, 'confirmed_compatible')))
    assert _criticality(subtitle, ctx, {})[0] == 'PRIMARY'
    subtitle = replace(subtitle, compatibility=((100, 'automatic_match'),))
    assert _criticality(subtitle, ctx, {})[0] == 'LOW'
    d06 = PlanRecord('confirmed_no_match', 9, 'old/orphan.ass', 'Show/orphan.ass', 'MOVE', 'WARNING')
    assert _criticality(d06, ctx, {})[0] == 'PRIMARY'


def test_story_preview_and_pv_source_evidence_have_different_severity():
    from app.target_execution_manifest import _criticality
    from app.target_planner_types import PlanRecord
    ctx, _ = fixture()
    record = PlanRecord('video', 100, 'old/source.mkv', 'Show/a.mkv', 'MOVE', 'READY')
    titles = dict(ctx.layout.titles)
    titles[10] = replace(titles[10], interpretation='story_preview')
    story = replace(ctx, layout=replace(ctx.layout, titles=titles),
        videos=(replace(ctx.videos[0], content_type='preview'),))
    assert _criticality(record, story, {})[0] == 'PRIMARY'
    promo = replace(ctx, videos=(replace(ctx.videos[0], content_type='preview', source_evidence_filename='PV01.mkv'),))
    assert _criticality(record, promo, {})[0] == 'LOW'


@pytest.mark.parametrize('standalone_name', ['Mini Dra', 'Standalone Bonus'])
def test_confirmed_own_title_metadata_makes_bonus_primary(standalone_name):
    from app.target_execution_manifest import _criticality
    from app.target_planner_types import PlanRecord
    ctx, _ = fixture(content='bonus')
    titles = dict(ctx.naming.titles)
    titles[10] = replace(titles[10], romaji=standalone_name)
    ctx = replace(ctx, naming=replace(ctx.naming, titles=titles))
    record = PlanRecord('video', 100, 'old/source.mkv', 'Show/a.mkv', 'MOVE', 'READY')
    assert _criticality(record, ctx, {})[0] == 'PRIMARY'


def test_manifest_reports_graph_and_readonly_runtime_observations(tmp_path):
    from app.tools.execution_manifest import observe_runtime
    manifest = make()
    graph = manifest.payload.graph
    assert (graph.source_target_chains, graph.cycles, graph.swaps, graph.occupied_targets) == (0, 0, 0, 0)
    observations = observe_runtime(tmp_path)
    assert observations['free_bytes'] >= 0
    assert observations['mount_fstype']
    assert observations['mount_identity'].startswith(observations['mount_fstype'] + ':')
    assert observations['rename_noreplace'] in {'PLATFORM_SYMBOL_ONLY_MOUNT_NOT_PROBED', 'PLATFORM_UNAVAILABLE'}
    assert observations['safe_write_capability'] == 'MOUNT_NOT_PROBED'


def test_snapshot_binding_rejects_invalid_or_prebaseline_time():
    ctx, snap = fixture()
    manifest = make(ctx, snap)
    for timestamp in ('not-a-time', '2026-10-06T23:59:59Z'):
        result = preflight_execution_manifest(manifest, ctx, snap, DB,
            runtime=RuntimeEvidence(snapshot_capable=True, snapshot_id='snapshot',
                snapshot_manifest_id=manifest.manifest_id, snapshot_at=timestamp))
        assert result.outcome == 'NOT_READY'
        assert 'snapshot_time_invalid_or_prebaseline' in result.diagnostics


def test_preflight_reports_each_safety_check_and_current_readonly_mount():
    ctx, snap = fixture()
    manifest = make(ctx, snap)
    result = preflight_execution_manifest(manifest, ctx, snap, DB,
        runtime=RuntimeEvidence(mount_read_only_now=True))
    checks = dict(result.checks)
    for name in ('database_fingerprint', 'source_preconditions', 'target_namespace',
                 'directory_dependencies', 'manifest_projection', 'warning_acknowledgements',
                 'runtime_requirements'):
        assert name in checks
    assert 'current_mount_read_only' in result.diagnostics


def test_criticality_batch_uses_one_video_index_for_many_subtitles():
    from app.target_execution_manifest import criticality_for_records
    from app.target_planner_types import PlanRecord
    ctx, _ = fixture()
    class CountedVideos:
        def __init__(self, values):
            self.values = values
            self.iterations = 0
        def __iter__(self):
            self.iterations += 1
            return iter(self.values)
    videos = CountedVideos((replace(ctx.videos[0], content_type='op'),))
    ctx = replace(ctx, videos=videos)
    records = tuple(PlanRecord('subtitle', i, f'old/{i}.ass', f'Show/{i}.ass', 'MOVE', 'READY',
        compatibility=((100, 'automatic_match'),)) for i in range(1000))
    results = criticality_for_records(ctx, records, {})
    assert all(value[0] == 'LOW' for value in results.values())
    assert videos.iterations <= 2


def test_human_criticality_cannot_downgrade_episode_or_unmatched_subtitle():
    ctx, snap = fixture()
    with pytest.raises(ValueError, match='downgrade'):
        make(ctx, snap, criticality_decisions={('video', 100): ('LOW', 'promotional', 'operator')})
    from app.target_planner_types import UnmatchedSubtitle
    ctx = replace(ctx, unmatched=(UnmatchedSubtitle(5, 'old/orphan.ass', 'confirmed_no_match'),),
        locator_baselines=ctx.locator_baselines + (LocatorBaseline('unmatched_subtitle', 5, 'relative_path', 'old/orphan.ass'),))
    snap = replace(snap, entries=snap.entries + (FilesystemEntry('old/orphan.ass', 'regular', 3, 4),))
    with pytest.raises(ValueError, match='downgrade'):
        make(ctx, snap, criticality_decisions={('confirmed_no_match', 5): ('LOW', 'orphan', 'operator')})


def test_source_replaced_by_symlink_is_stale_with_symlink_diagnostic():
    ctx, snap = fixture()
    manifest = make(ctx, snap)
    changed = replace(snap, entries=(replace(snap.entries[0], kind='symlink', size=None, mtime_ns=None),))
    result = preflight_execution_manifest(manifest, ctx, changed, DB)
    assert result.outcome == 'STALE'
    assert 'source_symlink:old/source.mkv' in result.diagnostics


def test_manifest_duplicate_execution_evidence_survives_json_roundtrip():
    ctx, snap = fixture()
    secondary = replace(ctx.videos[0], id=101, source='old/copy.mkv',
        duplicate_primary_id=100, duplicate_validity='valid')
    ctx = replace(ctx, videos=ctx.videos + (secondary,), locator_baselines=ctx.locator_baselines + (
        LocatorBaseline('video', 101, 'relative_path', 'old/copy.mkv'),
        LocatorBaseline('video', 101, 'root_folder', 'old')))
    snap = replace(snap, entries=snap.entries + (FilesystemEntry('old/copy.mkv', 'regular', 8, 9),))
    manifest = make(ctx, snap)
    loaded = manifest_from_json(manifest_to_json(manifest))
    assert len(loaded.payload.duplicate_evidence) == 1
    evidence = loaded.payload.duplicate_evidence[0]
    assert (evidence.secondary_video_id, evidence.primary_video_id, evidence.validity) == (101, 100, 'valid')
    assert evidence.side_asset_accounting in {'COMPLETE', 'INCOMPLETE', 'UNKNOWN'}


@pytest.mark.parametrize('tamper', ['target', 'drop_action', 'patch', 'post_hash'])
def test_rehashed_manifest_cannot_claim_different_approved_projection(tamper):
    import hashlib
    ctx, snap = fixture()
    encoded = json.loads(manifest_to_json(make(ctx, snap)))
    payload = encoded['payload']
    if tamper == 'target':
        payload['actions'][0]['target'] = 'Show/Season 01/Other.mkv'
        payload['directories'] = ['Show', 'Show/Season 01']
    elif tamper == 'drop_action':
        payload['actions'] = []
        payload['directories'] = []
        payload['logical_move_bytes'] = 0
    elif tamper == 'patch':
        payload['locator_patches'][0]['after'] = 'Elsewhere'
    else:
        payload['expected_post_hash'] = '0'*64
    encoded['manifest_id'] = hashlib.sha256(json.dumps(payload, sort_keys=True,
        separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()
    if tamper == 'target':
        with pytest.raises(ValueError, match='action identity mismatch'):
            manifest_from_json(json.dumps(encoded))
    else:
        manifest = manifest_from_json(json.dumps(encoded))
        result = preflight_execution_manifest(manifest, ctx, snap, DB)
        assert result.outcome in {'NOT_READY', 'ERROR'}
        assert any('manifest_projection_mismatch' in d for d in result.diagnostics)


def test_explicit_bonus_criticality_actor_survives_preflight(tmp_path):
    ctx, snap = fixture(content='bonus')
    names = dict(ctx.naming.titles)
    names[10] = replace(names[10], metadata_identity=None)
    from app.physical_naming import NamingChoice, create_basis_snapshot
    from test_target_planner import NOW
    naming = replace(ctx.naming, titles=names)
    choices = dict(naming.choices)
    choices[('title', 10)] = NamingChoice(2, 'Show', 'custom', NOW, create_basis_snapshot(naming, 'title', 10))
    ctx = replace(ctx, naming=replace(naming, choices=choices))
    decision = {('video', 100): ('PRIMARY', 'standalone_story', 'human_operator')}
    import shutil
    import sqlite3
    from app.tools.target_plan import database_fingerprint
    source_db = tmp_path / 'source.db'
    with sqlite3.connect(source_db) as db:
        db.execute('PRAGMA user_version=9')
    backup = tmp_path / 'backup.db'
    shutil.copyfile(source_db, backup)
    actual_db = database_fingerprint(source_db)
    manifest = build_execution_manifest(ctx, snap, plan_library(ctx, snap), actual_db,
        created_at='2026-10-07T00:00:00Z', criticality_decisions=decision, mount_identity='42:1')
    loaded = manifest_from_json(manifest_to_json(manifest))
    assert loaded.payload.criticality_authorities[0].actor == 'human_operator'
    runtime = RuntimeEvidence(scanner_paused_through_verification=True,
        inventory_writer_paused_through_verification=True, snapshot_id='snap1',
        snapshot_at='2026-10-07T01:00:00Z', snapshot_manifest_id=loaded.manifest_id,
        snapshot_capable=True, db_backup_path=str(backup), db_backup_sha256=actual_db['sha256'],
        db_backup_size=actual_db['size'], db_backup_user_version=actual_db['user_version'],
        db_backup_manifest_id=loaded.manifest_id, actual_windows_root='Z:\\Anime',
        mount_read_write_checked=True, safe_write_capability_proven=True,
        rename_noreplace_capability_proven=True, mount_identity='42:1', free_bytes=1000,
        mount_read_only_now=False)
    result = preflight_execution_manifest(loaded, ctx, snap, actual_db, runtime=runtime)
    assert result.outcome == 'READY_FOR_EXECUTION', result.diagnostics
    assert preflight_execution_manifest(loaded, ctx, snap, actual_db,
        runtime=replace(runtime, db_backup_path=str(tmp_path / 'missing.db'))).outcome == 'NOT_READY'
    hardlink = tmp_path / 'linked-source.db'
    hardlink.hardlink_to(source_db)
    hardlink_result = preflight_execution_manifest(loaded, ctx, snap, actual_db,
        runtime=replace(runtime, db_backup_path=str(hardlink)))
    assert hardlink_result.outcome == 'NOT_READY'
    assert 'db_backup_not_independent_copy' in hardlink_result.diagnostics


def test_case_only_source_to_target_needs_explicit_staging():
    source = 'show/Season 01/Show - S01E01.mkv'
    ctx, snap = fixture(source=source)
    manifest = make(ctx, snap)
    assert manifest.payload.graph.case_only == 1
    result = preflight_execution_manifest(manifest, ctx, snap, DB)
    assert result.outcome == 'NOT_READY'
    assert 'unresolved_move_graph' in result.diagnostics


def test_unicode_only_source_to_target_needs_explicit_staging():
    source = 'Cafe\u0301/Season 01/Show - S01E01.mkv'
    ctx, snap = fixture(source=source)
    choices = dict(ctx.naming.choices)
    choices[('collection', 1)] = replace(choices[('collection', 1)], physical_text='Café')
    ctx = replace(ctx, naming=replace(ctx.naming, choices=choices))
    manifest = make(ctx, snap)
    assert manifest.payload.graph.unicode_only == 1
    result = preflight_execution_manifest(manifest, ctx, snap, DB)
    assert result.outcome == 'NOT_READY'
    assert 'unresolved_move_graph' in result.diagnostics


def test_action_identity_preconditions_and_low_failure_contract():
    from app.target_execution_manifest import JournalEntry, can_continue_after_failure, journal_from_json, journal_to_json
    ctx, snap = fixture(content='op')
    manifest = make(ctx, snap)
    action = manifest.payload.actions[0]
    assert action.action_id and action.expected_source_kind == 'regular'
    assert action.expected_target_absent and action.expected_size == 7
    entry = JournalEntry(manifest.manifest_id, action.action_id, 0, 'FAILED', 'rename_io_error', True, True, True, True)
    assert journal_from_json(journal_to_json(entry)) == entry
    assert can_continue_after_failure(manifest, entry)
    assert not can_continue_after_failure(manifest, replace(entry, source_intact=False))
    assert not can_continue_after_failure(manifest, replace(entry, target_absent=False))
    assert not can_continue_after_failure(manifest, replace(entry, primary_dependencies_clear=False))
    assert not can_continue_after_failure(manifest, replace(entry, failure_journaled=False))
    encoded = json.loads(manifest_to_json(make()))
    encoded['payload']['delete'] = True
    with pytest.raises(ValueError):
        manifest_from_json(json.dumps(encoded))


def ready_runtime(tmp_path, manifest_id, actual_db, mount='42:1'):
    import shutil
    backup = tmp_path / 'backup.db'
    shutil.copyfile(tmp_path / 'source.db', backup)
    return RuntimeEvidence(scanner_paused_through_verification=True,
        inventory_writer_paused_through_verification=True, snapshot_id='snap1',
        snapshot_at='2026-10-07T01:00:00Z', snapshot_manifest_id=manifest_id, snapshot_capable=True,
        db_backup_path=str(backup), db_backup_sha256=actual_db['sha256'], db_backup_size=actual_db['size'],
        db_backup_user_version=actual_db['user_version'], db_backup_manifest_id=manifest_id,
        actual_windows_root='Z:\\Anime', mount_read_write_checked=True, safe_write_capability_proven=True,
        rename_noreplace_capability_proven=True, mount_identity=mount, free_bytes=1000, mount_read_only_now=False)


def actual_db(tmp_path):
    import sqlite3
    from app.tools.target_plan import database_fingerprint
    with sqlite3.connect(tmp_path / 'source.db') as db:
        db.execute('PRAGMA user_version=9')
    return database_fingerprint(tmp_path / 'source.db')


def test_unresolved_criticality_blocks_even_when_every_external_gate_is_proven(tmp_path):
    ctx, snap = fixture(content='bonus')
    names = dict(ctx.naming.titles)
    names[10] = replace(names[10], metadata_identity=None)
    from app.physical_naming import NamingChoice, create_basis_snapshot
    from test_target_planner import NOW
    naming = replace(ctx.naming, titles=names)
    choices = dict(naming.choices)
    choices[('title', 10)] = NamingChoice(2, 'Show', 'custom', NOW, create_basis_snapshot(naming, 'title', 10))
    ctx = replace(ctx, naming=replace(naming, choices=choices))
    db = actual_db(tmp_path)
    manifest = build_execution_manifest(ctx, snap, plan_library(ctx, snap), db,
        created_at='2026-10-07T00:00:00Z', mount_identity='42:1')
    result = preflight_execution_manifest(manifest, ctx, snap, db, runtime=ready_runtime(tmp_path, manifest.manifest_id, db))
    assert result.outcome == 'NOT_READY' and result.diagnostics == ('criticality_review_required',)


def test_acknowledging_one_warning_never_covers_another_of_the_same_class(tmp_path):
    from app.target_planner_types import UnmatchedSubtitle
    ctx, snap = fixture()
    ctx = replace(ctx, unmatched=(UnmatchedSubtitle(5, 'old/a.ass', 'confirmed_no_match'),
                                  UnmatchedSubtitle(6, 'old/b.ass', 'confirmed_no_match')),
        locator_baselines=ctx.locator_baselines + (LocatorBaseline('unmatched_subtitle', 5, 'relative_path', 'old/a.ass'),
                                                   LocatorBaseline('unmatched_subtitle', 6, 'relative_path', 'old/b.ass')))
    snap = replace(snap, entries=snap.entries + (FilesystemEntry('old/a.ass', 'regular', 3, 4), FilesystemEntry('old/b.ass', 'regular', 3, 4)))
    db = actual_db(tmp_path)
    manifest = build_execution_manifest(ctx, snap, plan_library(ctx, snap), db,
        created_at='2026-10-07T00:00:00Z', mount_identity='42:1')
    first, second = sorted(manifest.payload.warnings, key=lambda w: w.object_id)
    assert first.warning_class == second.warning_class
    ack = WarningAcknowledgement(first.warning_id, first.warning_class, first.object_kind, first.object_id,
                                 'operator', '2026-10-07T01:00:00Z')
    runtime = ready_runtime(tmp_path, manifest.manifest_id, db)
    result = preflight_execution_manifest(replace(manifest, acknowledgements=(ack,)), ctx, snap, db, runtime=runtime)
    assert result.outcome == 'NOT_READY' and result.diagnostics == ('warning_unacknowledged:' + second.warning_id,)
    both = (ack, replace(ack, warning_id=second.warning_id, object_id=second.object_id))
    assert preflight_execution_manifest(replace(manifest, acknowledgements=both), ctx, snap, db,
        runtime=runtime).outcome == 'READY_FOR_EXECUTION'


def test_rehashed_manifest_cannot_name_a_recycle_action():
    import hashlib
    encoded = json.loads(manifest_to_json(make()))
    encoded['payload']['actions'][0]['target'] = '#recycle/Show - S01E01.mkv'
    encoded['manifest_id'] = hashlib.sha256(json.dumps(encoded['payload'], sort_keys=True,
        separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()
    with pytest.raises(ValueError, match='system excluded action'):
        manifest_from_json(json.dumps(encoded))
