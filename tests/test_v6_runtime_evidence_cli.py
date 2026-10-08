"""External assertions cannot replace live observations or bypass preflight."""
from dataclasses import asdict
import json
import os
import sqlite3

import pytest

from app.models import Video
from app.target_execution_manifest import manifest_from_json
from app.target_planner_filesystem import inventory_filesystem
from app.tools import execution_manifest as cli
from app.tools.target_plan import database_fingerprint
from test_physical_naming import session, collection, title


WINDOWS_ROOT = r'\\192.168.11.149\Anime'
BOOL_FIELDS = (
    'scanner_paused_through_verification', 'inventory_writer_paused_through_verification',
    'snapshot_capable', 'mount_read_write_checked', 'safe_write_capability_proven',
    'rename_noreplace_capability_proven',
)
STRING_FIELDS = (
    'snapshot_id', 'snapshot_at', 'snapshot_manifest_id', 'db_backup_path',
    'db_backup_sha256', 'db_backup_manifest_id',
)
INT_FIELDS = ('db_backup_size', 'db_backup_user_version')
LIVE_FIELDS = ('actual_windows_root', 'mount_identity', 'mount_read_only_now', 'free_bytes')


@pytest.fixture
def case(session, tmp_path):
    library = tmp_path / 'library'
    media = library / 'Show' / 'release.mkv'
    media.parent.mkdir(parents=True)
    media.write_bytes(b'fixture')
    root = collection(session)
    main = title(session, root)
    session.add(Video(catalog_title=main, catalog_collection=root, root_folder='Show',
        relative_path='Show/release.mkv', filename='release.mkv', file_type='episode',
        size=media.stat().st_size, mtime_ns=media.stat().st_mtime_ns,
        local_episode_number=1, season_episode_number=1))
    session.commit()
    db = tmp_path / 'source.db'
    with sqlite3.connect(db) as destination:
        session.connection().connection.driver_connection.backup(destination)
    manifest_path = tmp_path / 'manifest.json'
    common = ['--db', str(db), '--library-root', str(library), '--windows-root', WINDOWS_ROOT]
    assert cli.main(['generate-manifest', *common, '--output', str(manifest_path)]) == 0
    manifest = manifest_from_json(manifest_path.read_text())
    backup = tmp_path / 'backup.db'
    backup.write_bytes(db.read_bytes())  # Only an independent scratch DB copy.
    fingerprint = database_fingerprint(db)
    evidence = {
        'scanner_paused_through_verification': True,
        'inventory_writer_paused_through_verification': True,
        'snapshot_capable': True,
        'snapshot_id': 'scratch-snapshot',
        'snapshot_at': manifest.created_at,
        'snapshot_manifest_id': manifest.manifest_id,
        'db_backup_path': str(backup),
        'db_backup_sha256': fingerprint['sha256'],
        'db_backup_size': fingerprint['size'],
        'db_backup_user_version': fingerprint['user_version'],
        'db_backup_manifest_id': manifest.manifest_id,
        'mount_read_write_checked': True,
        'safe_write_capability_proven': True,
        'rename_noreplace_capability_proven': True,
    }
    evidence_path = tmp_path / 'runtime-evidence.json'
    evidence_path.write_text(json.dumps(evidence))
    return common, db, library, manifest_path, evidence_path, evidence


def run(case, *, evidence=True, path=None, output=None):
    common, _, _, manifest_path, evidence_path, _ = case
    output = output or manifest_path.parent / 'dry-run.json'
    argv = ['dry-run', *common, '--manifest', str(manifest_path), '--output', str(output)]
    if evidence:
        argv += ['--runtime-evidence', str(path or evidence_path)]
    status = cli.main(argv)
    return status, json.loads(output.read_text())


def test_official_cli_composes_all_fields_and_reaches_ready_without_manifest_changes(case, monkeypatch):
    _, db, library, manifest_path, _, evidence = case
    manifest_bytes = manifest_path.read_bytes()
    manifest = manifest_from_json(manifest_bytes.decode())
    db_before, fs_before = database_fingerprint(db), inventory_filesystem(library)
    observations = []
    observe = cli.observe_runtime
    def capture_observation(root):
        observed = observe(root)
        observations.append(observed)
        return observed
    monkeypatch.setattr(cli, 'observe_runtime', capture_observation)
    seen = []
    preflight = cli.preflight_execution_manifest
    def capture(*args, **kwargs):
        seen.append(asdict(kwargs['runtime']))
        return preflight(*args, **kwargs)
    monkeypatch.setattr(cli, 'preflight_execution_manifest', capture)
    status, report = run(case)
    observed, = observations
    assert status == 0 and report['outcome'] == 'READY_FOR_EXECUTION'
    assert report['diagnostics'] == []
    assert seen == [{**evidence, 'actual_windows_root': WINDOWS_ROOT,
        'mount_identity': observed['mount_identity'],
        'mount_read_only_now': observed['mount_read_only_now'], 'free_bytes': observed['free_bytes']}]
    assert report['sql_select_count'] == 11 and report['filesystem_traversals'] == 1
    assert report['session_state'] == [0, 0, 0] and report['nas_write_operations'] == 0
    assert manifest_path.read_bytes() == manifest_bytes
    assert manifest_from_json(manifest_path.read_text()).payload == manifest.payload
    assert report['manifest_id'] == manifest.manifest_id
    assert database_fingerprint(db) == db_before
    assert inventory_filesystem(library) == fs_before


def test_no_runtime_file_keeps_external_assertions_unproven(case):
    status, report = run(case, evidence=False)
    assert status == 2 and report['outcome'] == 'NOT_READY'
    assert {'scanner_maintenance_not_proven', 'inventory_writer_maintenance_not_proven',
        'bound_snapshot_not_proven', 'bound_db_backup_not_proven', 'mount_read_write_not_proven',
        'safe_write_capability_not_proven', 'rename_noreplace_not_proven'} <= set(report['diagnostics'])


@pytest.mark.parametrize('field', BOOL_FIELDS + STRING_FIELDS + INT_FIELDS)
def test_missing_external_key_is_rejected_before_preflight(case, field, capsys):
    evidence = dict(case[-1])
    del evidence[field]
    case[-2].write_text(json.dumps(evidence))
    with pytest.raises(SystemExit) as error:
        run(case)
    assert error.value.code == 2
    assert 'runtime evidence' in capsys.readouterr().err.lower()
    assert not (case[3].parent / 'dry-run.json').exists()


@pytest.mark.parametrize('field', ('future_gate',) + LIVE_FIELDS)
def test_unknown_or_live_field_is_rejected(case, field, capsys):
    case[-2].write_text(json.dumps({**case[-1], field: False}))
    with pytest.raises(SystemExit) as error:
        run(case)
    assert error.value.code == 2
    assert 'runtime evidence' in capsys.readouterr().err.lower()
    assert not (case[3].parent / 'dry-run.json').exists()


@pytest.mark.parametrize('field,value',
    [(field, 1) for field in BOOL_FIELDS] + [(field, False) for field in STRING_FIELDS]
    + [(field, value) for field in INT_FIELDS for value in (True, 1.0, '9')])
def test_external_evidence_types_are_strict(case, field, value, capsys):
    case[-2].write_text(json.dumps({**case[-1], field: value}))
    with pytest.raises(SystemExit) as error:
        run(case)
    assert error.value.code == 2
    assert 'runtime evidence' in capsys.readouterr().err.lower()


@pytest.mark.parametrize('raw', ['{', '[]', 'null', 'true'])
def test_malformed_or_nonobject_json_is_rejected(case, raw, capsys):
    case[-2].write_text(raw)
    with pytest.raises(SystemExit) as error:
        run(case)
    assert error.value.code == 2
    assert 'runtime evidence' in capsys.readouterr().err.lower()


@pytest.mark.parametrize('kind', ['duplicate', 'nonfinite'])
def test_otherwise_complete_json_rejects_duplicate_keys_and_nonstandard_constants(case, kind, capsys):
    raw = (json.dumps(case[-1])[:-1] + ',"scanner_paused_through_verification":true}'
        if kind == 'duplicate' else json.dumps({**case[-1], 'db_backup_size': float('nan')}))
    case[-2].write_text(raw)
    with pytest.raises(SystemExit) as error:
        run(case)
    assert error.value.code == 2
    assert 'runtime evidence' in capsys.readouterr().err.lower()


@pytest.mark.parametrize('kind', ['missing', 'directory', 'fifo', 'symlink', 'parent_symlink',
    'library', 'library_parent_alias', 'hardlink', 'nas_root', 'remote_mount'])
def test_evidence_requires_local_regular_nonalias_file(case, kind, monkeypatch, capsys):
    _, _, library, _, evidence_path, evidence = case
    candidate = evidence_path.parent / 'candidate'
    if kind == 'directory':
        candidate.mkdir()
    elif kind == 'fifo':
        os.mkfifo(candidate)
    elif kind == 'symlink':
        candidate.symlink_to(evidence_path)
    elif kind == 'parent_symlink':
        candidate.symlink_to(evidence_path.parent, target_is_directory=True)
        candidate /= evidence_path.name
    elif kind == 'library':
        candidate = library / 'evidence.json'
        candidate.write_text(json.dumps(evidence))
    elif kind == 'library_parent_alias':
        candidate.symlink_to(library, target_is_directory=True)
        (library / 'evidence.json').write_text(json.dumps(evidence))
        candidate /= 'evidence.json'
    elif kind == 'hardlink':
        os.link(evidence_path, candidate)
    elif kind == 'nas_root':
        candidate = type(evidence_path)('/mnt/nas-anime/runtime-evidence.json')
    elif kind == 'remote_mount':
        candidate.write_text(json.dumps(evidence))
        mount_details = cli._mount_details
        monkeypatch.setattr(cli, '_mount_details', lambda path:
            ('cifs', '//scratch/remote') if path == candidate else mount_details(path))
    with pytest.raises(SystemExit) as error:
        run(case, path=candidate)
    assert error.value.code == 2
    message = capsys.readouterr().err.lower()
    assert 'runtime evidence' in message
    if kind in {'library', 'library_parent_alias', 'nas_root'}:
        # Rejected by location before any existence or type check.
        assert 'outside nas and library roots' in message
    assert not (case[3].parent / 'dry-run.json').exists()


@pytest.mark.parametrize('field,diagnostic', [
    ('snapshot_manifest_id', 'bound_snapshot_not_proven'),
    ('db_backup_manifest_id', 'bound_db_backup_not_proven'),
])
def test_cli_evidence_does_not_bypass_manifest_bindings(case, field, diagnostic):
    case[-2].write_text(json.dumps({**case[-1], field: '0' * 64}))
    status, report = run(case)
    assert status == 2 and report['outcome'] == 'NOT_READY'
    assert diagnostic in report['diagnostics']


@pytest.mark.parametrize('field,value,expected', [
    ('scanner_paused_through_verification', False, ('scanner_maintenance_not_proven',)),
    ('inventory_writer_paused_through_verification', False, ('inventory_writer_maintenance_not_proven',)),
    ('snapshot_capable', False, ('bound_snapshot_not_proven',)),
    ('snapshot_id', '', ('bound_snapshot_not_proven',)),
    ('snapshot_at', '2000-01-01T00:00:00Z', ('bound_snapshot_not_proven', 'snapshot_time_invalid_or_prebaseline')),
    ('db_backup_path', 'missing.db', ('db_backup_file_missing_or_not_regular',)),
    ('db_backup_path', 'linked.db', ('db_backup_not_independent_copy',)),
    ('db_backup_path', 'other.db', ('db_backup_bytes_mismatch',)),
    ('db_backup_sha256', '0' * 64, ('bound_db_backup_not_proven',)),
    ('db_backup_size', 0, ('bound_db_backup_not_proven',)),
    ('db_backup_user_version', 999, ('bound_db_backup_not_proven',)),
    ('mount_read_write_checked', False, ('mount_read_write_not_proven',)),
    ('safe_write_capability_proven', False, ('safe_write_capability_not_proven',)),
    ('rename_noreplace_capability_proven', False, ('rename_noreplace_not_proven',)),
])
def test_each_external_fact_fails_closed_alone(case, field, value, expected):
    _, db, _, _, evidence_path, evidence = case
    if field == 'db_backup_path':
        value = evidence_path.parent / value
        if value.name == 'linked.db':
            os.link(db, value)
        elif value.name == 'other.db':
            with sqlite3.connect(value) as other:
                other.execute(f"PRAGMA user_version={evidence['db_backup_user_version']}")
        value = str(value)
    evidence_path.write_text(json.dumps({**evidence, field: value}))
    status, report = run(case)
    assert (status, report['outcome'], tuple(report['diagnostics'])) == (2, 'NOT_READY', expected)


@pytest.mark.parametrize('change,outcome,expected', [
    ('mount_identity', 'NOT_READY', ('mount_identity_not_proven',)),
    ('free_bytes', 'NOT_READY', ('free_bytes_not_reported',)),
    ('no_windows_root', 'STALE', ('plan_hash_changed',)),
    ('over_budget_root', 'STALE', ('plan_hash_changed',)),
    ('source_changed', 'STALE', ('source_changed:Show/release.mkv',)),
    ('database_changed', 'STALE', ('database_fingerprint_changed',)),
])
def test_complete_evidence_cannot_mask_live_or_stale_facts(case, monkeypatch, change, outcome, expected):
    common, db, library = case[:3]
    observed = cli.observe_runtime(library)
    if change in {'mount_identity', 'free_bytes'}:
        monkeypatch.setattr(cli, 'observe_runtime', lambda root: {**observed, change: None})
    elif change == 'no_windows_root':
        common = common[:-2]
    elif change == 'over_budget_root':
        common = [*common[:-1], WINDOWS_ROOT + '\\' + 'x' * 230]
    elif change == 'source_changed':
        media = library / 'Show' / 'release.mkv'
        os.utime(media, ns=(media.stat().st_atime_ns, media.stat().st_mtime_ns + 1000))
    else:
        with sqlite3.connect(db) as changed:
            changed.execute('PRAGMA user_version=77')
    status, report = run((common, *case[1:]))
    assert (status, report['outcome'], tuple(report['diagnostics'])) == (2, outcome, expected)


def test_external_capability_claims_cannot_turn_observed_ro_into_rw(case, monkeypatch):
    observed = cli.observe_runtime(case[2])
    monkeypatch.setattr(cli, 'observe_runtime', lambda root: {**observed, 'mount_read_only_now': True})
    status, report = run(case)
    assert status == 2 and report['outcome'] == 'NOT_READY'
    assert 'current_mount_read_only' in report['diagnostics']


def test_explicit_false_and_null_external_fields_remain_unproven(case):
    case[-2].write_text(json.dumps({**dict.fromkeys(BOOL_FIELDS, False),
        **dict.fromkeys(STRING_FIELDS + INT_FIELDS, None)}))
    status, report = run(case)
    assert status == 2 and report['outcome'] == 'NOT_READY'
    assert 'bound_snapshot_not_proven' in report['diagnostics']
    assert 'scanner_maintenance_not_proven' in report['diagnostics']


def test_report_cannot_overwrite_runtime_evidence_input(case):
    before = case[-2].read_bytes()
    with pytest.raises(ValueError, match='replace an input file'):
        run(case, output=case[-2])
    assert case[-2].read_bytes() == before
