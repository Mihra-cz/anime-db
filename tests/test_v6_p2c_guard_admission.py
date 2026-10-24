"""R3-F1: no caller-root I/O; binding, compatibility and real process barriers."""
from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import select
import sqlite3
import subprocess
import sys

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.database import Base, make_engine
from app.maintenance import MaintenanceBusy, ScratchMaintenance
from app.scanner import LibrarySafetyError, scan_library
from app import target_execution_guard as guard
from test_scanner import PROBE_RESULT
from test_v6_p2c_execution_guard import small_case, guard_path, snapshot
from test_v6_p2c_rescan import apply_approved_post_state
from v6_database_case import database_rows


def ordinary_case(tmp_path):
    database = tmp_path / 'data' / 'anime.db'
    root = tmp_path / 'library'
    root.mkdir(mode=0o700)
    (root / 'Show - 01.mkv').write_bytes(b'synthetic media')
    engine = make_engine(f'sqlite:///{database}')
    Base.metadata.create_all(engine)
    engine.dispose()
    return database, root


@contextmanager
def actor(mode, *arguments):
    helper = Path(__file__).with_name('v6_execution_guard_helper.py').absolute()
    project = helper.parent.parent
    env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(project), str(helper.parent))))
    process = subprocess.Popen([sys.executable, str(helper), mode, *map(str, arguments)],
        cwd=project, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, bufsize=0)
    try:
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
        process.stdin.close()
        process.stdout.close()
        process.stderr.close()


def receive(process, timeout=30):
    assert select.select([process.stdout], [], [], timeout)[0], 'actor did not reach its IPC barrier'
    line = process.stdout.readline()
    assert line, process.stderr.read()
    return json.loads(line)


def command(process, value):
    process.stdin.write((value + '\n').encode())
    process.stdin.flush()


def test_major1_blocking_nas_metadata_stays_in_healthcheck_worker(tmp_path):
    with actor('nas_probe', tmp_path) as process:
        events = []
        while True:
            item = receive(process)
            events.append(item)
            assert item['event'] != 'PARENT_ROOT_FS', item
            if item['event'] == 'DONE':
                break
        assert process.wait(timeout=10) == 0, process.stderr.read()
    assert {item['event'] for item in events} >= {
        'APP', 'STARTUP', 'POST_TIMEOUT', 'DIRECT_TIMEOUT', 'WORKER_ROOT_FS', 'DONE'}
    parent = next(item['pid'] for item in events if item['event'] == 'APP')
    assert all(item['pid'] != parent for item in events if item['event'] == 'WORKER_ROOT_FS')


@pytest.mark.parametrize('hardlink', [False, True])
def test_legacy_database_without_execution_accepts_hardlinks(tmp_path, monkeypatch, hardlink):
    path, root = ordinary_case(tmp_path)
    if hardlink:
        os.link(path, path.with_name('backup.db'))
    monkeypatch.setattr('app.scanner.service.probe_video', lambda *a, **k: PROBE_RESULT)
    engine = make_engine(f'sqlite:///{path}')
    try:
        with Session(engine) as session:
            assert scan_library(session, root).created == 1
        before = database_rows(path)
        with Session(engine) as session:
            assert scan_library(session, root, confirm_deletions=True).unchanged == 1
        assert database_rows(path) == before
    finally:
        engine.dispose()


@pytest.mark.parametrize('kind', ['ordinary', 'scratch_named'])
def test_production_namespace_does_not_activate_scratch_or_consume_lock(tmp_path, monkeypatch, kind):
    path, root = ordinary_case(tmp_path)
    name = 'anime.db' if kind == 'ordinary' else 'v6-e2a-' + 'a' * 32 + '.sqlite'
    production = Path('/var/lib/animedb/data') / name
    monkeypatch.setattr(guard, '_database_path', lambda bind: production)
    touched = []
    def present(candidate):
        touched.append(candidate)
        return candidate.name.endswith('.maintenance.lock')
    monkeypatch.setattr(guard, '_present', present)
    engine = make_engine(f'sqlite:///{path}')
    try:
        with guard.ordinary_scan_admission(engine, root):
            pass
        assert all(not p.name.endswith('.maintenance.lock') for p in touched)
        assert all('.journal.' not in p.name for p in touched)
    finally:
        engine.dispose()


@pytest.mark.parametrize('shape', ['directory', 'fifo'])
def test_nonregular_inactive_database_is_not_a_new_scratch_policy(tmp_path, shape):
    path = tmp_path / 'data' / 'anime.db'
    path.parent.mkdir()
    if shape == 'directory':
        path.mkdir()
    else:
        os.mkfifo(path)
    # No SQLite open of a FIFO: this pins admission's boundary, not SQLite's
    # handling of an unsupported file. The DB driver owns the eventual error.
    engine = make_engine(f'sqlite:///{path}')
    try:
        with guard.ordinary_scan_admission(engine, tmp_path / 'library'):
            pass
    finally:
        engine.dispose()


@pytest.mark.parametrize('suffix', ['../elsewhere', 'child/../library'])
def test_parent_traversal_is_rejected_lexically_without_root_io(tmp_path, monkeypatch, suffix):
    engine = make_engine('sqlite://')
    root = tmp_path / suffix
    def forbidden(*args, **kwargs):
        pytest.fail('resolve consulted the library filesystem')
    monkeypatch.setattr(Path, 'resolve', forbidden)
    try:
        with pytest.raises(guard.ExecutionGuardStopped):
            with guard.ordinary_scan_admission(engine, root):
                pytest.fail('parent traversal was admitted')
    finally:
        engine.dispose()


@pytest.mark.parametrize('order', ['scanner_first', 'executor_first'])
def test_wrong_root_cannot_bind_fresh_scratch_database(small_case, order):
    owner, path, _ = small_case
    wrong = path.parent / 'foreign'
    wrong.mkdir(mode=0o700)
    if order == 'executor_first':
        ScratchMaintenance(path, owner.root)
    engine = make_engine(f'sqlite:///{path}')
    before = database_rows(path)
    try:
        with pytest.raises(guard.ExecutionGuardStopped):
            with guard.ordinary_scan_admission(engine, wrong):
                pytest.fail('wrong root reached scan admission')
        with guard.ordinary_scan_admission(engine, owner.root):
            pass
        assert database_rows(path) == before
    finally:
        engine.dispose()


def test_m24_explicit_r1_plain_engine_ignores_ordinary_guard(small_case):
    owner, path, manifest = small_case
    apply_approved_post_state(owner.root, path, manifest)
    guard_path(path).write_bytes(b'persisted interrupted/review marker')
    before, content, physical = database_rows(path), path.read_bytes(), snapshot(owner.root)
    statements = []
    engine = make_engine(f'sqlite:///{path}')
    event.listen(engine, 'before_cursor_execute',
        lambda c, cur, sql, params, ctx, many: statements.append(sql))
    try:
        with Session(engine) as session:
            result = scan_library(session, owner.root, execution_manifest=manifest)
            assert (result.found, result.unchanged, result.created, result.removed) == (2, 2, 0, 0)
        assert all(sql.lstrip().upper().startswith(('SELECT', 'PRAGMA')) for sql in statements)
        assert database_rows(path) == before and path.read_bytes() == content
        assert snapshot(owner.root) == physical
    finally:
        engine.dispose()


def test_m02_second_check_after_lease_stops_crashed_executor(tmp_path):
    # The executor must own the original fresh E7 capability in its own PID.
    with actor('crash_wait', tmp_path) as executor:
        info = receive(executor)
        path, root = Path(info['database']), Path(info['root'])
        before, content = database_rows(path), path.read_bytes()
        with actor('paused_scan', path, root) as scanner:
            assert receive(scanner)['event'] == 'CHECKED'
            command(executor, 'EXECUTE')
            import signal
            assert executor.wait(timeout=90) == -signal.SIGKILL, executor.stderr.read()
            first = next(action for action in info['actions'] if action['action'] != 'KEEP')
            assert (root / first['target']).is_file() and not (root / first['source']).exists()
            assert guard_path(path).is_file()
            physical = snapshot(root)
            assert database_rows(path) == before and path.read_bytes() == content
            command(scanner, 'CONTINUE')
            result = receive(scanner)
            assert scanner.wait(timeout=30) == 0, scanner.stderr.read()
        # These checks deliberately precede the returned STOP status: removing
        # the second check permits real create/delete under confirm_deletions.
        assert database_rows(path) == before, 'ordinary scan changed Video identities after the crash'
        assert path.read_bytes() == content and snapshot(root) == physical
        assert result['event'] == 'STOP' and 'Reorganization' in result['reason']


def test_m16_plain_engine_discovers_exclusive_existing_e6b_binding(tmp_path, monkeypatch):
    path, root = ordinary_case(tmp_path)
    path.chmod(0o600)
    path.parent.chmod(0o700)
    bound_root = path.parent / 'library'
    root.rename(bound_root)
    root = bound_root
    before, content, physical = database_rows(path), path.read_bytes(), snapshot(root)
    monkeypatch.setattr('app.scanner.service.probe_video', lambda *a, **k: PROBE_RESULT)
    engine = make_engine(f'sqlite:///{path}')
    try:
        with actor('hold_writer', path, root) as owner:
            assert receive(owner)['event'] == 'HELD'
            stopped = False
            try:
                with Session(engine) as session:
                    scan_library(session, root, confirm_deletions=True)
            except MaintenanceBusy:
                stopped = True
            assert owner.poll() is None
            command(owner, 'RELEASE')
            assert owner.wait(timeout=15) == 0, owner.stderr.read()
        assert database_rows(path) == before, 'plain scanner wrote through an exclusive E6B lease'
        assert path.read_bytes() == content and snapshot(root) == physical
        assert stopped, 'plain scanner bypassed existing maintenance discovery'
    finally:
        engine.dispose()


@pytest.mark.parametrize('wrong', [False, True])
def test_two_process_executor_first_uses_db_bound_root(small_case, wrong):
    owner, path, _ = small_case
    target = owner.root if not wrong else path.parent / 'foreign'
    if wrong:
        target.mkdir(mode=0o700)
    engine = make_engine(f'sqlite:///{path}')
    before = database_rows(path)
    try:
        with actor('hold_writer', path, owner.root) as holder:
            assert receive(holder)['event'] == 'HELD'
            error = guard.ExecutionGuardStopped if wrong else MaintenanceBusy
            with pytest.raises(error):
                with guard.ordinary_scan_admission(engine, target):
                    pytest.fail('executor-first namespace did not exclude the scanner')
            command(holder, 'RELEASE')
            assert holder.wait(timeout=15) == 0
        assert database_rows(path) == before
    finally:
        engine.dispose()


def test_two_process_scanner_first_excludes_executor(small_case):
    owner, path, _ = small_case
    engine = make_engine(f'sqlite:///{path}')
    try:
        with guard.ordinary_scan_admission(engine, owner.root):
            with actor('try_writer', path, owner.root) as executor:
                assert receive(executor)['event'] == 'BUSY'
                assert executor.wait(timeout=15) == 0
    finally:
        engine.dispose()


@pytest.mark.parametrize('exception', [OSError('ordinary scanner I/O failure'), ValueError('ordinary scanner validation')])
def test_scanner_exceptions_are_not_reclassified_as_execution_errors(tmp_path, monkeypatch, exception):
    path, root = ordinary_case(tmp_path)
    def failed(*args, **kwargs):
        raise exception
    monkeypatch.setattr('app.scanner.service._scan_library', failed)
    engine = make_engine(f'sqlite:///{path}')
    try:
        with Session(engine) as session, pytest.raises(type(exception)) as caught:
            scan_library(session, root)
        assert caught.value is exception
    finally:
        engine.dispose()


def test_unreadable_relevant_marker_namespace_stays_fail_closed(tmp_path, monkeypatch):
    path, root = ordinary_case(tmp_path)
    original = guard._present
    def failed(candidate):
        if candidate == guard_path(path):
            raise PermissionError('cannot inspect the reserved execution guard namespace')
        return original(candidate)
    monkeypatch.setattr(guard, '_present', failed)
    engine = make_engine(f'sqlite:///{path}')
    try:
        with pytest.raises(guard.ExecutionGuardStopped):
            with guard.ordinary_scan_admission(engine, root):
                pytest.fail('unreadable guard namespace was admitted')
    finally:
        engine.dispose()


@pytest.mark.parametrize('uri', [False, True])
def test_unusual_valid_legacy_database_path_keeps_sqlite_semantics(tmp_path, monkeypatch, uri):
    from urllib.parse import quote
    monkeypatch.chdir(tmp_path)
    path, root = ordinary_case(tmp_path)
    unusual = path.with_name('anime odd # č %.db')
    path.rename(unusual)
    url = (f'sqlite:///file:{quote(str(unusual), safe="/")}?mode=rw&uri=true'
        if uri else f'sqlite:///{unusual}')
    engine = make_engine(url)
    monkeypatch.setattr('app.scanner.service.probe_video', lambda *a, **k: PROBE_RESULT)
    try:
        with Session(engine) as session:
            assert scan_library(session, root).created == 1
        with closing(sqlite3.connect('file:' + quote(str(unusual), safe='/') + '?mode=ro', uri=True)) as db:
            assert db.execute('SELECT count(*) FROM videos').fetchone()[0] == 1
    finally:
        engine.dispose()


@pytest.mark.parametrize('evidence', ['same_inode_without_evidence', 'other_inode_with_marker'])
def test_legacy_hardlink_needs_related_execution_evidence(tmp_path, monkeypatch, evidence):
    path, root = ordinary_case(tmp_path)
    os.link(path, path.with_name('backup.db'))
    candidate = path.with_name('v6-e2a-' + 'b' * 32 + '.sqlite')
    if evidence == 'same_inode_without_evidence':
        os.link(path, candidate)
    else:
        candidate.write_bytes(path.read_bytes())
        guard_path(candidate).write_bytes(b'foreign evidence belongs to another inode')
    monkeypatch.setattr('app.scanner.service.probe_video', lambda *a, **k: PROBE_RESULT)
    engine = make_engine(f'sqlite:///{path}')
    try:
        with Session(engine) as session:
            assert scan_library(session, root).created == 1
        assert len(database_rows(path)['videos']) == 1
        assert not candidate.with_name(candidate.name + '.maintenance.lock').exists()
    finally:
        engine.dispose()
