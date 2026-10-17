"""E6B admission tests use only explicit private scratch database/root paths."""
from pathlib import Path
from contextlib import closing
import importlib
import importlib.util
import os
import sqlite3

import pytest
from sqlalchemy import text

from app.database import make_engine, make_session_factory


def api():
    assert importlib.util.find_spec('app.maintenance') is not None, 'E6B boundary is missing'
    return importlib.import_module('app.maintenance')


@pytest.fixture
def binding(tmp_path):
    root = tmp_path / 'library'
    root.mkdir(mode=0o700)
    database = tmp_path / 'scratch.sqlite'
    with closing(sqlite3.connect(database)) as db:
        with db:
            db.execute('CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)')
            db.execute("INSERT INTO sample VALUES (1, 'original')")
    database.chmod(0o600)
    gate = api().ScratchMaintenance(database, root)
    return gate


def test_shared_writers_and_exclusive_maintenance(binding):
    gate = binding
    other = api().ScratchMaintenance(gate.database_path, gate.library_root)
    assert gate.lock_path == other.lock_path
    with gate.writer(), other.writer():
        with pytest.raises(api().MaintenanceBusy):
            with gate.maintenance():
                pytest.fail('maintenance entered during writers')
    with gate.maintenance():
        for enter in (other.writer, other.maintenance):
            with pytest.raises(api().MaintenanceBusy):
                with enter():
                    pytest.fail('exclusive owner bypassed')
    with other.writer():
        pass


@pytest.mark.parametrize('finish', ['commit', 'rollback'])
def test_engine_lease_spans_transaction_completion(binding, finish):
    gate = binding
    engine = make_engine(f'sqlite:///{gate.database_path}', maintenance=gate)
    try:
        with make_session_factory(engine)() as session:
            session.execute(text("UPDATE sample SET value='pending' WHERE id=1"))
            with pytest.raises(api().MaintenanceBusy):
                with gate.maintenance():
                    pytest.fail('maintenance entered active transaction')
            getattr(session, finish)()
            with gate.maintenance():
                pass
        with engine.connect() as connection:
            assert connection.scalar(text('SELECT value FROM sample')) == ('pending' if finish == 'commit' else 'original')
    finally:
        engine.dispose()


def test_readonly_session_is_available_but_cannot_write_during_maintenance(binding):
    gate = binding
    engine = make_engine(f'sqlite:///{gate.database_path}', maintenance=gate)
    sessions = make_session_factory(engine)
    try:
        with gate.maintenance():
            with sessions(readonly=True) as session:
                assert session.scalar(text('SELECT value FROM sample')) == 'original'
                with pytest.raises(Exception, match='readonly|read-only'):
                    session.execute(text("UPDATE sample SET value='forbidden'"))
            with sessions() as session:
                with pytest.raises(api().MaintenanceBusy):
                    session.execute(text("UPDATE sample SET value='forbidden'"))
    finally:
        engine.dispose()


def test_owner_database_scope_is_explicit_and_expires(binding):
    gate = binding
    engine = make_engine(f'sqlite:///{gate.database_path}', maintenance=gate)
    try:
        with gate.maintenance() as owner:
            with pytest.raises(api().MaintenanceBusy):
                with engine.begin():
                    pytest.fail('implicit exclusive owner write admission')
            with owner.write_scope():
                with engine.begin() as connection:
                    connection.execute(text("UPDATE sample SET value='owner'"))
        with pytest.raises(api().MaintenanceStopped):
            with owner.write_scope():
                pytest.fail('closed exclusive capability reused')
    finally:
        engine.dispose()


def test_readonly_request_cannot_be_overridden_and_connection_is_reset(binding):
    gate = binding
    engine = make_engine(f'sqlite:///{gate.database_path}', maintenance=gate)
    sessions = make_session_factory(engine)
    try:
        with gate.maintenance(), api().readonly_request():
            with sessions(readonly=False) as session:
                assert session.scalar(text('SELECT value FROM sample')) == 'original'
                with pytest.raises(Exception, match='readonly|read-only'):
                    session.execute(text("UPDATE sample SET value='forbidden'"))
        with engine.connect() as connection:
            # Public migrations also use DBAPI before SQLAlchemy emits begin.
            dbapi = connection.connection.dbapi_connection
            assert dbapi.execute('PRAGMA query_only').fetchone()[0] == 0
    finally:
        engine.dispose()


def test_owner_cannot_release_exclusion_with_an_active_borrowed_transaction(binding):
    gate = binding
    engine = make_engine(f'sqlite:///{gate.database_path}', maintenance=gate)
    sessions = make_session_factory(engine)
    admission = gate.maintenance()
    owner = admission.__enter__()
    session = sessions()
    try:
        with owner.write_scope():
            session.execute(text("UPDATE sample SET value='uncommitted'"))
        with pytest.raises(api().MaintenanceStopped, match='active DB transaction'):
            admission.__exit__(None, None, None)
        with pytest.raises(api().MaintenanceBusy):
            with gate.writer():
                pytest.fail('owner exclusion released before borrowed transaction ended')
        session.rollback()
        with gate.writer():
            pass
    finally:
        session.close()
        engine.dispose()


def test_namespace_rebinding_and_replacement_are_rejected(binding):
    gate = binding
    other_root = gate.library_root.parent / 'different-library'
    other_root.mkdir(mode=0o700)
    with pytest.raises(api().MaintenanceStopped):
        api().ScratchMaintenance(gate.database_path, other_root)
    with pytest.raises(api().MaintenanceStopped), gate.writer():
        original = gate.lock_path.with_suffix('.original')
        gate.lock_path.rename(original)
        gate.lock_path.write_bytes(original.read_bytes())
        gate.lock_path.chmod(0o600)
        with pytest.raises(api().MaintenanceStopped):
            with gate.maintenance():
                pytest.fail('replacement namespace accepted')


def worker(gate, mode):
    import subprocess
    import sys
    environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]),
        PYTHONDONTWRITEBYTECODE='1', ANIME_PATH='/tmp', DATABASE_URL='sqlite://',
        METADATA_DOWNLOAD_ARTWORK='false')
    return subprocess.Popen([sys.executable, str(Path(__file__).with_name('v6_maintenance_helper.py')),
        mode, str(gate.database_path), str(gate.library_root)], text=True,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment)


def line(process):
    import select
    assert select.select([process.stdout], [], [], 10)[0], 'worker did not report admission'
    return process.stdout.readline().strip()


def finish(process, command='END'):
    output, errors = process.communicate(command + '\n', timeout=10)
    assert process.returncode == 0, (output, errors)
    assert output.strip() == 'DONE'


def dispose_worker(process):
    if process.poll() is None:
        process.kill()
    process.communicate(timeout=10)


def test_independent_shared_writers_exclude_maintenance(binding, request):
    gate = binding
    first, second = worker(gate, 'shared'), worker(gate, 'shared')
    for process in (first, second):
        request.addfinalizer(lambda p=process: dispose_worker(p))
    assert line(first) == line(second) == 'READY'
    exclusive = worker(gate, 'exclusive')
    request.addfinalizer(lambda: dispose_worker(exclusive))
    output, errors = exclusive.communicate(timeout=10)
    assert exclusive.returncode == 3 and output.strip() == 'BUSY', errors
    finish(first)
    with pytest.raises(api().MaintenanceBusy):
        with gate.maintenance():
            pytest.fail('second writer ignored')
    finish(second)
    with gate.maintenance():
        pass


@pytest.mark.parametrize('finish_transaction', ['COMMIT', 'ROLLBACK'])
def test_real_process_transaction_blocks_maintenance_through_completion(binding, request, finish_transaction):
    gate = binding
    process = worker(gate, 'transaction')
    request.addfinalizer(lambda: dispose_worker(process))
    assert line(process) == 'READY'
    with pytest.raises(api().MaintenanceBusy):
        with gate.maintenance():
            pytest.fail('active transaction ignored')
    finish(process, finish_transaction)
    with gate.maintenance(), closing(sqlite3.connect(gate.database_path)) as db:
        expected = 'writer committed' if finish_transaction == 'COMMIT' else 'original'
        assert db.execute('SELECT value FROM sample').fetchone()[0] == expected


def test_exclusive_owner_rejects_other_processes_and_sigkill_is_not_recovery(binding, request):
    gate = binding
    process = worker(gate, 'owner_db')
    request.addfinalizer(lambda: dispose_worker(process))
    assert line(process) == 'READY'
    for mode in ('shared', 'transaction', 'exclusive'):
        contender = worker(gate, mode)
        request.addfinalizer(lambda p=contender: dispose_worker(p))
        output, errors = contender.communicate(timeout=10)
        assert contender.returncode == 3 and output.strip() == 'BUSY', errors
    marker = gate.library_root / 'unfinished-operation'
    before = gate.database_path.read_bytes(), marker.read_bytes()
    process.kill()  # SIGKILL must release the kernel lease, never certify state.
    process.wait(timeout=10)
    with gate.maintenance():
        assert (gate.database_path.read_bytes(), marker.read_bytes()) == before
    with gate.writer():
        pass
    assert marker.read_text() == 'unverified physical state'


def test_fork_child_cannot_own_close_or_keep_parent_exclusion(binding):
    gate = binding
    ready_r, ready_w = os.pipe()
    release_r, release_w = os.pipe()
    child = None
    try:
        with gate.maintenance() as owner:
            child = os.fork()
            if child == 0:
                try:
                    os.close(ready_r)
                    os.close(release_w)
                    for operation in (owner.close, lambda: gate._acquire()):
                        try:
                            operation()
                        except api().MaintenanceStopped:
                            pass
                        else:
                            os._exit(4)
                    os.write(ready_w, b'READY')
                    os.read(release_r, 1)
                    os._exit(0)
                except BaseException:
                    os._exit(5)
            os.close(ready_w)
            ready_w = None
            os.close(release_r)
            release_r = None
            import select
            assert select.select([ready_r], [], [], 10)[0]
            assert os.read(ready_r, 5) == b'READY'
            with pytest.raises(api().MaintenanceBusy):
                with gate.writer():
                    pytest.fail('child close released parent lock')
        # Child stays alive: it must not retain an inherited lock description.
        with gate.maintenance():
            pass
        os.write(release_w, b'X')
        _, status = os.waitpid(child, 0)
        child = None
        assert status == 0
    finally:
        if child is not None:
            import signal
            os.kill(child, signal.SIGKILL)
            os.waitpid(child, 0)
        for fd in (ready_r, ready_w, release_r, release_w):
            if fd is not None:
                os.close(fd)


def test_lease_success_rejection_and_body_failures_do_not_leak_fds(binding, recwarn):
    import gc
    gate = binding
    gc.collect()
    before = set(os.listdir('/proc/self/fd'))
    for _ in range(5):
        with gate.writer():
            with pytest.raises(api().MaintenanceBusy):
                with gate.maintenance():
                    pytest.fail('writer not protected')
        with pytest.raises(ValueError, match='body'):
            with gate.maintenance():
                raise ValueError('body')
    gc.collect()
    assert set(os.listdir('/proc/self/fd')) == before
    assert not [w for w in recwarn if issubclass(w.category, ResourceWarning)]


def test_option_engine_keeps_binding_and_scanner_root_validation(binding):
    from app.maintenance import writer_admission
    gate = binding
    engine = make_engine(f'sqlite:///{gate.database_path}', maintenance=gate)
    option = engine.execution_options(stream_results=True)
    try:
        with gate.maintenance(), pytest.raises(api().MaintenanceBusy):
            with writer_admission(option):
                pytest.fail('option engine lost maintenance binding')
    finally:
        engine.dispose()


def test_inherited_active_sqlalchemy_transaction_has_no_write_or_close_authority(binding):
    gate = binding
    engine = make_engine(f'sqlite:///{gate.database_path}', maintenance=gate)
    session = make_session_factory(engine)()
    session.execute(text("UPDATE sample SET value='parent pending'"))
    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:
        try:
            os.close(read_fd)
            operations = [lambda: session.execute(text("UPDATE sample SET value='child'")),
                session.commit, session.rollback, session.close]
            stopped = 0
            for operation in operations:
                try:
                    operation()
                except api().MaintenanceStopped:
                    stopped += 1
            os.write(write_fd, str(stopped).encode())
            os._exit(0)
        except BaseException:
            os._exit(5)
    os.close(write_fd)
    try:
        import select
        assert select.select([read_fd], [], [], 10)[0]
        result = os.read(read_fd, 8)
        _, status = os.waitpid(child, 0)
        assert status == 0 and result == b'4'
        with pytest.raises(api().MaintenanceBusy):
            with gate.maintenance():
                pytest.fail('child released parent transaction admission')
        session.commit()
    finally:
        os.close(read_fd)
        session.close()
        engine.dispose()


def test_unexpected_lock_failure_stops_and_preserves_cause_without_fd_leak(binding, monkeypatch):
    import errno
    gate = binding
    before = set(os.listdir('/proc/self/fd'))
    failure = OSError(errno.EIO, 'synthetic flock failure')
    def failed(*args):
        raise failure
    monkeypatch.setattr(api().fcntl, 'flock', failed)
    with pytest.raises(api().MaintenanceStopped) as error:
        with gate.writer():
            pytest.fail('failed lock granted admission')
    assert error.value.__cause__ is failure
    assert set(os.listdir('/proc/self/fd')) == before


@pytest.mark.parametrize('database,root', [('/outside/scratch.sqlite', '/outside/library'),
    ('relative.sqlite', '/tmp/library')])
def test_non_scratch_binding_is_rejected_before_filesystem_access(monkeypatch, database, root):
    def forbidden(*args):
        pytest.fail('non-scratch path was observed')
    monkeypatch.setattr(api(), '_read_mounts', forbidden)
    with pytest.raises(api().MaintenanceStopped):
        api().ScratchMaintenance(Path(database), Path(root))


def _b1_rows(path):
    with closing(sqlite3.connect(f'file:{path}?mode=ro', uri=True)) as database:
        return database.execute('SELECT id, value FROM sample ORDER BY id').fetchall()


@pytest.mark.parametrize('driver', [False, True], ids=['execute', 'exec_driver_sql'])
@pytest.mark.parametrize('first,second', [
    ("UPDATE sample SET value='first'", "UPDATE sample SET value='bypassed'"),
    ('SELECT value FROM sample', "INSERT INTO sample VALUES (2, 'bypassed')"),
], ids=['update_then_update', 'select_then_insert'])
def test_b1_rejected_core_autobegin_cannot_execute_again(binding, driver, first, second):
    from sqlalchemy import event
    gate = binding
    engine = make_engine(f'sqlite:///{gate.database_path}', maintenance=gate)
    before = gate.database_path.read_bytes()
    reached_cursor = []
    event.listen(engine, 'do_execute', lambda cursor, sql, parameters, context: reached_cursor.append(sql))
    try:
        with engine.connect() as connection, gate.maintenance():
            execute = connection.exec_driver_sql if driver else lambda sql: connection.execute(text(sql))
            with pytest.raises(api().MaintenanceBusy):
                execute(first)
            with pytest.raises(api().MaintenanceStopped):
                execute(second)
            assert reached_cursor == []
            assert _b1_rows(gate.database_path) == [(1, 'original')]
            assert gate.database_path.read_bytes() == before
        with gate.maintenance():
            pass
    finally:
        engine.dispose()
    for fd in os.listdir('/proc/self/fd'):
        try:
            target = os.readlink(f'/proc/self/fd/{fd}')
        except FileNotFoundError:
            continue
        assert not target.startswith(str(gate.database_path.parent) + '/')


@pytest.mark.parametrize('stale', ['closed', 'foreign_connection', 'placeholder'])
def test_b1_connection_info_cannot_supply_stale_or_foreign_lease(binding, stale):
    from sqlalchemy import event
    gate = binding
    engine = make_engine(f'sqlite:///{gate.database_path}', maintenance=gate)
    reached_cursor = []
    try:
        with engine.connect() as foreign, engine.connect() as connection:
            assert foreign.scalar(text('SELECT value FROM sample')) == 'original'
            assert connection.scalar(text('SELECT value FROM sample')) == 'original'
            if stale == 'closed':
                connection.info['maintenance_lease'].close()
            elif stale == 'foreign_connection':
                connection.info['maintenance_lease'] = foreign.info['maintenance_lease']
            else:
                connection.info['maintenance_lease'] = object()
            event.listen(engine, 'do_execute', lambda cursor, sql, p, context: reached_cursor.append(sql))
            with pytest.raises(api().MaintenanceStopped):
                connection.execute(text("UPDATE sample SET value='bypassed'"))
            assert not reached_cursor
            connection.close()
            assert foreign.scalar(text('SELECT value FROM sample')) == 'original'
        with gate.maintenance():
            pass
        assert _b1_rows(gate.database_path) == [(1, 'original')]
    finally:
        engine.dispose()


def test_b1_expired_owner_scope_cannot_reuse_borrowed_connection(binding):
    gate = binding
    engine = make_engine(f'sqlite:///{gate.database_path}', maintenance=gate)
    try:
        with gate.maintenance() as owner, engine.connect() as connection:
            with owner.write_scope():
                assert connection.scalar(text('SELECT value FROM sample')) == 'original'
            with pytest.raises(api().MaintenanceStopped):
                connection.execute(text("UPDATE sample SET value='bypassed'"))
        assert _b1_rows(gate.database_path) == [(1, 'original')]
    finally:
        engine.dispose()


def test_b1_readonly_option_cannot_authorize_a_rejected_writer(binding):
    gate = binding
    engine = make_engine(f'sqlite:///{gate.database_path}', maintenance=gate)
    before = gate.database_path.read_bytes()
    try:
        with engine.connect() as connection, gate.maintenance():
            with pytest.raises(api().MaintenanceBusy):
                connection.execute(text('SELECT value FROM sample'))
            connection.execution_options(scratch_readonly=True)
            with pytest.raises(api().MaintenanceStopped):
                connection.exec_driver_sql("UPDATE sample SET value='bypassed'")
            assert gate.database_path.read_bytes() == before
        with gate.maintenance(), engine.connect() as connection:
            connection.execution_options(scratch_readonly=True)
            with pytest.raises(api().MaintenanceStopped):
                connection.exec_driver_sql('SELECT value FROM sample')
    finally:
        engine.dispose()


def test_b1_authorized_readonly_keeps_query_only_enforcement(binding):
    from sqlalchemy.exc import OperationalError
    gate = binding
    engine = make_engine(f'sqlite:///{gate.database_path}', maintenance=gate)
    try:
        with gate.maintenance(), make_session_factory(engine)(readonly=True) as session:
            assert session.scalar(text('SELECT value FROM sample')) == 'original'
            with pytest.raises(OperationalError, match='readonly|read-only'):
                session.execute(text("UPDATE sample SET value='bypassed'"))
            connection = session.connection()
            connection.exec_driver_sql('PRAGMA query_only=OFF')
            with pytest.raises(api().MaintenanceStopped):
                connection.exec_driver_sql("INSERT INTO sample VALUES (2, 'bypassed')")
        assert _b1_rows(gate.database_path) == [(1, 'original')]
    finally:
        engine.dispose()


@pytest.mark.parametrize('invalidate', [False, True], ids=['close', 'invalidate'])
def test_b1_authorized_core_commit_rollback_and_savepoint_work(binding, invalidate):
    gate = binding
    engine = make_engine(f'sqlite:///{gate.database_path}', maintenance=gate)
    try:
        with engine.connect() as connection:
            connection.execute(text("UPDATE sample SET value='committed'"))
            connection.commit()
            connection.exec_driver_sql("UPDATE sample SET value='rolled back'")
            connection.rollback()
            assert connection.scalar(text('SELECT value FROM sample')) == 'committed'
            nested = connection.begin_nested()
            connection.execute(text("UPDATE sample SET value='savepoint'"))
            nested.rollback()
            connection.commit()
            if invalidate:
                connection.invalidate()
        with gate.maintenance():
            assert _b1_rows(gate.database_path) == [(1, 'committed')]
    finally:
        engine.dispose()


def test_b1_session_rejection_repeated_without_unleased_sql(binding):
    from sqlalchemy.exc import InvalidRequestError
    gate = binding
    engine = make_engine(f'sqlite:///{gate.database_path}', maintenance=gate)
    try:
        with make_session_factory(engine)() as session:
            with gate.maintenance():
                with pytest.raises(api().MaintenanceBusy):
                    session.execute(text("UPDATE sample SET value='first'"))
                with pytest.raises((api().MaintenanceStopped, InvalidRequestError)):
                    session.execute(text("UPDATE sample SET value='bypassed'"))
                session.rollback()
                with pytest.raises(api().MaintenanceBusy):
                    session.execute(text("INSERT INTO sample VALUES (2, 'bypassed')"))
                session.rollback()
                assert _b1_rows(gate.database_path) == [(1, 'original')]
            session.execute(text("UPDATE sample SET value='authorized'"))
            session.commit()
        with gate.maintenance():
            assert _b1_rows(gate.database_path) == [(1, 'authorized')]
    finally:
        engine.dispose()


def test_b1_session_flush_savepoint_commit_and_rollback(binding):
    from sqlalchemy import Column, Integer, String
    from sqlalchemy.exc import PendingRollbackError
    from sqlalchemy.orm import DeclarativeBase
    class LocalBase(DeclarativeBase):
        pass
    class Sample(LocalBase):
        __tablename__ = 'sample'
        id = Column(Integer, primary_key=True)
        value = Column(String)
    gate = binding
    engine = make_engine(f'sqlite:///{gate.database_path}', maintenance=gate)
    try:
        with make_session_factory(engine)() as session:
            with gate.maintenance():
                session.add(Sample(id=2, value='blocked'))
                with pytest.raises(api().MaintenanceBusy):
                    session.flush()
                with pytest.raises(PendingRollbackError):
                    session.execute(text('SELECT value FROM sample'))
                session.rollback()
                assert _b1_rows(gate.database_path) == [(1, 'original')]
            added = Sample(id=2, value='committed')
            session.add(added)
            session.flush()
            nested = session.begin_nested()
            added.value = 'nested'
            session.flush()
            nested.rollback()
            session.commit()
            added.value = 'outer rollback'
            session.flush()
            session.rollback()
        with gate.maintenance():
            assert _b1_rows(gate.database_path) == [(1, 'original'), (2, 'committed')]
    finally:
        engine.dispose()
