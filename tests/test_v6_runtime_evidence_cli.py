"""External assertions cannot replace live observations or bypass preflight."""
from dataclasses import asdict
import errno
import gc
import io
import json
import os
from pathlib import Path
import resource
import sqlite3
import subprocess
import sys

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


def _isolate_nas_paths(monkeypatch, tmp_path):
    # Exercise the same location guard without resolving a production mount.
    nas = Path('/mnt/nas-anime')
    def scratch_path(value, *parts):
        path = Path(value, *parts)
        return tmp_path / 'denied-nas' / path.relative_to(nas) if path == nas or nas in path.parents else path
    monkeypatch.setattr(cli, 'Path', scratch_path)


@pytest.fixture(autouse=True)
def isolated_nas_paths(monkeypatch, tmp_path):
    _isolate_nas_paths(monkeypatch, tmp_path)


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


def _reader_child(tmp_path, scenario, kind='io'):
    code = '''
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]).parent))
from test_v6_runtime_evidence_cli import _reader_case
_reader_case(Path(sys.argv[2]), *sys.argv[3:])
'''
    parent = tmp_path / 'parent-owner'
    parent.write_bytes(b'parent descriptor')
    with parent.open('rb') as stream:
        result = subprocess.run([sys.executable, '-Werror::ResourceWarning', '-c', code,
            str(Path(__file__).resolve()), str(tmp_path), scenario, kind],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'DATABASE_URL': 'sqlite://',
                'ANIME_PATH': str(tmp_path / 'library'), 'METADATA_DOWNLOAD_ARTWORK': 'false'},
            capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stderr == ''
        # This is printed only after all child assertions, including fd checks.
        assert result.stdout == f'runtime reader verified: {scenario} {kind}\n'
        assert stream.read() == b'parent descriptor'


def test_runtime_reader_valid_utf8_evidence_stays_readonly(tmp_path):
    _reader_child(tmp_path, 'valid')


@pytest.mark.parametrize('scenario', ('early', 'repeated', 'open_error', 'closed_allocation', 'parser_error'))
@pytest.mark.parametrize('kind', ('io', 'memory', 'keyboard', 'exit'))
def test_runtime_reader_fault_preserves_ownership_and_exception(tmp_path, scenario, kind):
    _reader_child(tmp_path, scenario, kind)


def test_runtime_reader_late_native_text_failure_preserves_recycled_fd(tmp_path):
    _reader_child(tmp_path, 'late')


@pytest.mark.parametrize('scenario', ('directory_swap', 'symlink_swap', 'fifo_swap'))
def test_runtime_reader_rejects_changed_leaf_without_leaking_fd(tmp_path, scenario):
    _reader_child(tmp_path, scenario)


@pytest.mark.parametrize('scenario', ('symlink', 'hardlink', 'invalid_json'))
def test_runtime_reader_guard_failure_closes_owned_stream(tmp_path, scenario):
    _reader_child(tmp_path, scenario)


def _reader_case(tmp_path, scenario, kind):
    target = tmp_path / 'evidence.json'
    evidence = {**dict.fromkeys(BOOL_FIELDS, True),
                **dict.fromkeys(STRING_FIELDS, 'scratch-Žluťoučký'),
                **dict.fromkeys(INT_FIELDS, 1)}
    target.write_text('{' if scenario == 'invalid_json' else json.dumps(evidence, ensure_ascii=False),
                      encoding='utf-8')
    before_bytes, before_stat = target.read_bytes(), target.stat()
    foreign_path = tmp_path / 'foreign-owner'
    foreign_path.write_bytes(b'foreign descriptor')
    if scenario == 'symlink':
        target.rename(tmp_path / 'original')
        target.symlink_to(foreign_path)
    if scenario == 'hardlink':
        os.link(target, tmp_path / 'second-link')
    primary = (OSError(errno.EIO, 'reader fault') if kind == 'io' else
               MemoryError('reader fault') if kind == 'memory' else
               KeyboardInterrupt('reader fault') if kind == 'keyboard' else SystemExit(113))
    native_open, native_io_open = os.open, io.open
    native_parser = cli._strict_json_load
    opens, streams, recycled, outcomes = [], [], [], []
    armed = False
    constructor_faults = 0

    def reject_fileio(event, args):
        nonlocal constructor_faults
        # Native FileIO audits before adopting an existing fd or invoking opener.
        if (armed and scenario in {'early', 'repeated'} and event == 'open' and args[1] == 'r'
                and (isinstance(args[0], int) or args[0] == str(target))):
            constructor_faults += 1
            raise primary
    sys.addaudithook(reject_fileio)

    def opened(path, flags, *args, **kwargs):
        assert os.fspath(path) == str(target), 'reader changed its input path'
        assert flags == os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        opens.append(None)
        if scenario == 'open_error':
            raise primary
        if scenario in {'directory_swap', 'symlink_swap', 'fifo_swap'}:
            target.rename(tmp_path / 'original')
            if scenario == 'directory_swap':
                target.mkdir()
            elif scenario == 'symlink_swap':
                target.symlink_to(foreign_path)
            else:
                os.mkfifo(target)
        fd = native_open(path, flags, *args, **kwargs)
        opens[-1] = fd
        return fd

    def open_stream(file, *args, **kwargs):
        nonlocal primary
        if (not args or args[0] != 'r'
                or (not isinstance(file, int) and os.fspath(file) != str(target))):
            return native_io_open(file, *args, **kwargs)
        if scenario == 'late':
            # A real TextIOWrapper failure follows successful raw/buffer creation.
            arguments, options = list(args), dict(kwargs)
            if len(arguments) >= 3:
                arguments[2] = 'missing-v6-test-codec'
            else:
                options['encoding'] = 'missing-v6-test-codec'
            try:
                native_io_open(file, *arguments, **options)
            except LookupError as exc:
                primary = exc
            else:
                raise AssertionError('native text construction did not fail')
        else:
            stream = native_io_open(file, *args, **kwargs)
            streams.append(stream)
            if scenario != 'closed_allocation':
                return stream
            stream.close()
        replacement = io.FileIO(foreign_path, 'rb')
        recycled.append(replacement)
        assert replacement.fileno() == opens[-1]
        raise primary

    def parse(raw):
        assert raw == before_bytes.decode('utf-8')
        if scenario == 'parser_error':
            raise primary
        return native_parser(raw)

    def forbid_numeric_close(fd):
        raise AssertionError('reader attempted numeric close/retry')

    limits = resource.getrlimit(resource.RLIMIT_NOFILE)
    with io.FileIO(foreign_path, 'rb') as foreign:
        def fd_inventory():
            result = {}
            for name in os.listdir('/proc/self/fd'):
                try:
                    result[int(name)] = os.readlink(f'/proc/self/fd/{name}')
                except FileNotFoundError:
                    pass  # os.listdir's own fd is already closed.
            return result
        before = fd_inventory()
        try:
            if scenario == 'repeated':
                resource.setrlimit(resource.RLIMIT_NOFILE, (max(before) + 12, limits[1]))
            with pytest.MonkeyPatch.context() as injected:
                _isolate_nas_paths(injected, tmp_path)
                injected.setattr(os, 'open', opened)
                injected.setattr(io, 'open', open_stream)
                injected.setattr(os, 'close', forbid_numeric_close)
                injected.setattr(cli, '_strict_json_load', parse)
                armed = True
                for _ in range(32 if scenario == 'repeated' else 1):
                    result = error = None
                    try:
                        result = cli._read_runtime_evidence(target, tmp_path / 'library')
                    except BaseException as exc:
                        error = exc
                    outcomes.append((result, error))
        finally:
            armed = False
            resource.setrlimit(resource.RLIMIT_NOFILE, limits)
        gc.collect()
        after = fd_inventory()
        expected = {**before, **{s.fileno(): str(foreign_path) for s in recycled}}
        assert after == expected, ('runtime evidence fd leak', len(after) - len(expected))
        for stream in recycled:
            assert stream.read() == b'foreign descriptor'
            stream.close()  # The stream object owns the deliberately reused fd.
        assert fd_inventory() == before
        assert foreign.read() == b'foreign descriptor'
        assert all(stream.closed for stream in streams)

    for result, error in outcomes:
        if scenario == 'valid':
            assert result == evidence and len(result) == 14 and error is None
        elif scenario in {'early', 'repeated', 'open_error', 'closed_allocation', 'parser_error', 'late'}:
            assert error is primary and error.__traceback__ is not None
            if kind == 'exit':
                assert error.code == 113
        elif scenario in {'directory_swap', 'symlink_swap'}:
            assert isinstance(error, OSError)
            assert error.errno == (errno.EISDIR if scenario == 'directory_swap' else errno.ELOOP)
        else:
            assert isinstance(error, ValueError)
            if scenario in {'hardlink', 'fifo_swap'}:
                assert str(error) == 'input must be an independent regular file'
            if scenario == 'symlink':
                assert 'symlink' in str(error)
    if scenario in {'early', 'repeated'}:
        assert constructor_faults == len(outcomes) and not opens
    if scenario == 'symlink':
        assert not opens
    original = tmp_path / 'original' if scenario in {'symlink', 'directory_swap', 'symlink_swap', 'fifo_swap'} else target
    assert original.read_bytes() == before_bytes
    assert (original.stat().st_size, original.stat().st_mtime_ns) == (before_stat.st_size, before_stat.st_mtime_ns)
    print(f'runtime reader verified: {scenario} {kind}')
