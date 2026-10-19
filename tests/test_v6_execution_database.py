"""E5A integrates only a single scratch MOVE and two exact SQLite locators."""
from dataclasses import replace
import builtins
import errno
import gc
import io
import os
from pathlib import Path
import sqlite3

import pytest

from app import target_execution_coordination as coordination
from app import target_execution_database as database_api
from app import target_execution_filesystem as fs
from app import target_execution_identity as identity
from app import target_execution_journal as journal
from app.target_execution_coordination import ScratchActionSession
from app.target_execution_journal import load_journal
from app.target_execution_manifest import LocatorPatch, manifest_to_json, post_state_hash_from_manifest
from app.target_planner_filesystem import inventory_filesystem
from app.target_planner_service import load_planner_context, readonly_planner_session
from v6_database_case import prepare_database, database_rows, legacy_v1
from test_v6_execution_filesystem import seal, changed_action
from app.tools.target_plan import database_fingerprint


@pytest.fixture
def case(tmp_path):
    with ScratchActionSession.create(tmp_path) as session:
        yield session, *prepare_database(session)


def execute(session, manifest):
    return session.execute_video_move(manifest, manifest.payload.actions[0], action_index=0)


def states(session, manifest):
    if not session.journal_path.exists():
        return []
    return [e.state for e in load_journal(session.journal_path, manifest).entries]


def reseal_database(manifest, path):
    return seal(manifest, db_fingerprint=type(manifest.payload.db_fingerprint)(**database_fingerprint(path)))


def sql(path, statement, args=()):
    connection = sqlite3.connect(path)
    try:
        connection.execute(statement, args)
        connection.commit()
    finally:
        connection.close()


def test_verified_move_updates_only_approved_video_locators_and_completes(tmp_path):
    with ScratchActionSession.create(tmp_path) as session:
        manifest, source, target, database = prepare_database(session)
        before, original_manifest = database_rows(database), manifest_to_json(manifest)
        execute = getattr(session, 'execute_video_move', None)
        assert callable(execute), 'E5A single-video DB integration is missing'
        result = execute(manifest, manifest.payload.actions[0], action_index=0)
        assert result.outcome == 'COMPLETE'
        assert result.video_id == 100 and result.database_path == database
        assert not source.exists() and target.read_bytes() == b'original scratch video'
        assert [e.state for e in load_journal(session.journal_path, manifest).entries] == [
            'PENDING', 'FS_APPLIED', 'FS_VERIFIED', 'DB_RECONCILED', 'COMPLETE']
        after = database_rows(database)
        from app.models import Video
        fields = list(Video.__table__.columns.keys())
        expected = []
        for row in before['videos']:
            row = list(row)
            if row[fields.index('id')] == 100:
                row[fields.index('relative_path')] = target.relative_to(session.root).as_posix()
                row[fields.index('root_folder')] = 'Show'
            expected.append(tuple(row))
        assert after == {**before, 'videos': tuple(expected)}
        with readonly_planner_session(database) as db:
            context = load_planner_context(db)
        assert post_state_hash_from_manifest(manifest, context, inventory_filesystem(session.root)) == manifest.payload.expected_post_hash
        assert manifest_to_json(manifest) == original_manifest


def test_one_writer_owns_the_entire_lifecycle_and_commit_precedes_db_ack(case, monkeypatch):
    session, manifest, source, target, path = case
    created, appended, committed = [], [], []
    create, append, commit = journal.DurableJournal.create, journal.DurableJournal.append, database_api._ScratchVideoDatabase._commit
    def created_writer(cls, *args, **kwargs):
        writer = create(*args, **kwargs)
        created.append(writer)
        return writer
    def committed_db(db):
        commit(db)
        committed.append(True)
    def appended_state(writer, entry):
        assert writer is created[0]
        if entry.state in {'DB_RECONCILED', 'COMPLETE'}:
            assert committed == [True] and target.exists() and not source.exists()
            with readonly_planner_session(path) as db:
                actual = load_planner_context(db)
            assert next(v for v in actual.videos if v.id == 100).source == manifest.payload.actions[0].target
        append(writer, entry)
        appended.append(entry.state)
    monkeypatch.setattr(journal.DurableJournal, 'create', classmethod(created_writer))
    monkeypatch.setattr(journal.DurableJournal, 'append', appended_state)
    monkeypatch.setattr(database_api._ScratchVideoDatabase, '_commit', committed_db)
    assert execute(session, manifest).outcome == 'COMPLETE'
    assert len(created) == 1 and created[0]._closed
    assert appended == ['PENDING', 'FS_APPLIED', 'FS_VERIFIED', 'DB_RECONCILED', 'COMPLETE']


def test_historical_v1_stops_before_db_evidence_journal_or_rename(case, monkeypatch):
    session, manifest, source, target, path = case
    legacy = legacy_v1(manifest)
    before, rows = path.read_bytes(), database_rows(path)
    calls = []
    open_db, capture = database_api._ScratchVideoDatabase._open, identity.ScratchIdentitySession.capture
    create, rename = journal.DurableJournal.create, fs._rename_noreplace
    monkeypatch.setattr(database_api._ScratchVideoDatabase, '_open', lambda db: calls.append('db') or open_db(db))
    monkeypatch.setattr(identity.ScratchIdentitySession, 'capture',
        lambda owner, *args, **kwargs: calls.append('evidence') or capture(owner, *args, **kwargs))
    monkeypatch.setattr(journal.DurableJournal, 'create',
        classmethod(lambda cls, *args, **kwargs: calls.append('journal') or create(*args, **kwargs)))
    monkeypatch.setattr(fs, '_rename_noreplace', lambda *args: calls.append('rename') or rename(*args))
    with pytest.raises(coordination.ScratchCoordinationStopped) as error:
        execute(session, legacy)
    assert calls == []
    assert source.exists() and not target.exists()
    assert path.read_bytes() == before and database_rows(path) == rows
    assert not session.journal_path.exists() and not session.evidence_path.exists()
    assert type(error.value) is coordination.ScratchCoordinationStopped
    assert error.value.phase == 'VALIDATION' and not error.value.reconciliation_required
    assert 'legacy manifest has no sealed Windows root' in str(error.value.__cause__)


@pytest.mark.parametrize('version', [1, 2])
def test_foreign_library_root_is_rejected_without_foreign_path_io(case, tmp_path, monkeypatch, version):
    session, manifest, source, target, path = case
    if version == 1:
        manifest = legacy_v1(manifest)
    foreign = tmp_path / 'foreign-library'
    foreign.mkdir()
    manifest = seal(manifest, library_root=str(foreign))
    before = path.read_bytes()
    foreign_io = []

    def watch(original, operation):
        def observed(value, *args, **kwargs):
            if not isinstance(value, int):
                candidate = Path(os.fsdecode(value))
                if not candidate.is_absolute() and kwargs.get('dir_fd') is not None:
                    candidate = Path(os.readlink(f'/proc/self/fd/{kwargs["dir_fd"]}')) / candidate
                candidate = Path(os.path.abspath(candidate))
                if candidate == foreign or foreign in candidate.parents:
                    foreign_io.append((operation, str(candidate)))
            return original(value, *args, **kwargs)
        return observed

    # Start observing only after the synthetic manifest and fixture are ready.
    with monkeypatch.context() as execution:
        for module, operation in ((os, 'lstat'), (os, 'stat'), (os, 'open'),
                (builtins, 'open'), (io, 'open')):
            execution.setattr(module, operation, watch(getattr(module, operation), operation))
        with pytest.raises(coordination.ScratchCoordinationStopped) as error:
            execute(session, manifest)

    assert foreign_io == []
    assert type(error.value) is coordination.ScratchDatabaseReconciliationRequired
    assert error.value.phase == 'DB_VALIDATION' and error.value.reconciliation_required
    assert isinstance(error.value.__cause__, fs.RenameRejected)
    assert 'root/mount binding mismatch' in str(error.value.__cause__)
    assert source.exists() and not target.exists() and path.read_bytes() == before
    assert not session.journal_path.exists() and not session.evidence_path.exists()


def test_bound_v2_root_reaches_e5a_final_verification(tmp_path):
    with ScratchActionSession.create(tmp_path) as session:
        manifest, source, target, path = prepare_database(session, windows_root=r'C:\Anime')
        assert manifest.payload.version == 2 and manifest.payload.windows_root == r'C:\Anime'
        before = database_rows(path)
        result = execute(session, manifest)
        assert result.outcome == 'COMPLETE'
        assert not source.exists() and target.read_bytes() == b'original scratch video'
        assert states(session, manifest) == ['PENDING', 'FS_APPLIED', 'FS_VERIFIED', 'DB_RECONCILED', 'COMPLETE']
        from app.models import Video
        action, fields = manifest.payload.actions[0], list(Video.__table__.columns.keys())
        changed = {'relative_path': action.target, 'root_folder': action.target.split('/')[0]}
        expected = tuple(tuple(changed.get(name, value) if row[fields.index('id')] == 100 else value
            for name, value in zip(fields, row)) for row in before['videos'])
        assert database_rows(path) == {**before, 'videos': expected}


def test_known_rename_rejection_leaves_db_bytes_unchanged(case, monkeypatch):
    session, manifest, source, target, path = case
    before, rows = path.read_bytes(), database_rows(path)
    def rejected(*args):
        raise OSError(errno.EACCES, 'test rename denied')
    monkeypatch.setattr(fs, '_rename_noreplace', rejected)
    with pytest.raises(coordination.ScratchCoordinationStopped):
        execute(session, manifest)
    assert source.exists() and not target.exists()
    assert path.read_bytes() == before and database_rows(path) == rows
    assert states(session, manifest) == ['PENDING'] and session.evidence_path.exists()


@pytest.mark.parametrize('after_apply', [False, True])
def test_failure_after_fs_verification_aborts_uncommitted_db_without_physical_rollback(case, monkeypatch, after_apply):
    session, manifest, source, target, path = case
    before = database_rows(path)
    apply = database_api._ScratchVideoDatabase._apply
    def fail(db):
        if after_apply:
            apply(db)
        raise OSError(errno.EIO, 'test DB failure before commit')
    monkeypatch.setattr(database_api._ScratchVideoDatabase, '_apply', fail)
    with pytest.raises(coordination.ScratchDatabaseReconciliationRequired, match='before commit'):
        execute(session, manifest)
    assert not source.exists() and target.read_bytes() == b'original scratch video'
    assert database_rows(path) == before
    assert states(session, manifest) == ['PENDING', 'FS_APPLIED', 'FS_VERIFIED']
    assert session.evidence_path.exists()


@pytest.mark.parametrize('trigger', ['abort', 'mutate_manual'])
def test_real_sql_failure_or_trigger_side_effect_cannot_partially_commit(case, trigger):
    session, manifest, source, target, path = case
    body = "SELECT RAISE(ABORT,'root patch rejected');" if trigger == 'abort' else \
        "UPDATE videos SET filename='forbidden' WHERE id=100;"
    sql(path, 'CREATE TRIGGER test_patch BEFORE UPDATE OF root_folder ON videos BEGIN ' + body + ' END')
    manifest = reseal_database(manifest, path)
    before = database_rows(path)
    with pytest.raises(coordination.ScratchDatabaseReconciliationRequired):
        execute(session, manifest)
    assert database_rows(path) == before
    assert not source.exists() and target.exists()
    assert states(session, manifest) == ['PENDING', 'FS_APPLIED', 'FS_VERIFIED']


@pytest.mark.parametrize('fresh_fingerprint', [False, True])
def test_original_db_value_conflict_stops_before_rename(case, fresh_fingerprint):
    session, manifest, source, target, path = case
    sql(path, "UPDATE videos SET root_folder='changed' WHERE id=100")
    if fresh_fingerprint:
        manifest = reseal_database(manifest, path)
    before = path.read_bytes()
    with pytest.raises(coordination.ScratchDatabaseReconciliationRequired):
        execute(session, manifest)
    assert source.exists() and not target.exists() and path.read_bytes() == before
    assert not session.evidence_path.exists() and not session.journal_path.exists()


@pytest.mark.parametrize('change', ['id', 'missing', 'wrong_before', 'wrong_after', 'foreign_video',
    'collection', 'title', 'subtitle', 'filename', 'no_post_hash', 'keep', 'quarantine'])
def test_wrong_action_or_dependent_patches_are_never_inferred_or_applied(case, change):
    session, manifest, source, target, path = case
    patches = manifest.payload.locator_patches
    if change == 'id':
        manifest = seal(manifest, (changed_action(manifest, object_id=200),))
    elif change in {'keep', 'quarantine'}:
        action = changed_action(manifest, action=change.upper(),
            target=manifest.payload.actions[0].source if change == 'keep' else manifest.payload.actions[0].target,
            expected_target_absent=change != 'keep')
        manifest = seal(manifest, (action,))
    elif change == 'no_post_hash':
        manifest = seal(manifest, expected_post_hash=None)
    else:
        if change == 'missing':
            patches = tuple(p for p in patches if p.field != 'root_folder')
        elif change in {'wrong_before', 'wrong_after'}:
            patches = tuple(replace(p, **{'before' if change == 'wrong_before' else 'after': 'wrong'})
                if p.field == 'relative_path' else p for p in patches)
        elif change == 'filename':
            patches += (LocatorPatch('video', 100, 'filename', 'source.mkv', 'target.mkv'),)
        else:
            kind = 'video' if change == 'foreign_video' else change
            patches += (LocatorPatch(kind, 200 if kind == 'video' else 1,
                'relative_path' if kind in {'video', 'subtitle'} else 'relative_root_path', 'old', 'Show'),)
        manifest = seal(manifest, locator_patches=patches)
    before = path.read_bytes()
    with pytest.raises(coordination.ScratchCoordinationStopped):
        execute(session, manifest)
    assert source.exists() and not target.exists() and path.read_bytes() == before
    assert not session.journal_path.exists() and not session.evidence_path.exists()


def test_other_db_row_change_invalidates_the_manifest_even_when_source_locators_match(case):
    session, manifest, source, target, path = case
    sql(path, "UPDATE videos SET filename='new user decision' WHERE id=200")
    before = database_rows(path)
    with pytest.raises(coordination.ScratchDatabaseReconciliationRequired, match='fingerprint'):
        execute(session, manifest)
    assert database_rows(path) == before and source.exists() and not target.exists()


def test_root_folder_can_stay_unchanged_without_inventing_a_patch(case):
    session, manifest, source, target, path = case
    same_root_source = session.root / 'Show/release.mkv'
    source.rename(same_root_source)
    sql(path, "UPDATE videos SET relative_path='Show/release.mkv',root_folder='Show' WHERE id=100")
    action = changed_action(manifest, source='Show/release.mkv')
    patches = tuple(replace(p, before='Show/release.mkv') for p in manifest.payload.locator_patches if p.field == 'relative_path')
    manifest = reseal_database(seal(manifest, (action,), locator_patches=patches), path)
    assert execute(session, manifest).outcome == 'COMPLETE'
    assert target.exists() and not same_root_source.exists()


@pytest.mark.parametrize('fault', ['missing', 'empty', 'wal', 'hot_journal'])
def test_unsupported_sqlite_is_rejected_before_a_read_write_connection(case, monkeypatch, fault):
    session, manifest, source, target, path = case
    if fault == 'missing':
        path.unlink()
    elif fault == 'empty':
        path.write_bytes(b'')
    elif fault == 'wal':
        connection = sqlite3.connect(path)
        try:
            assert connection.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
        finally:
            connection.close()
    else:
        path.with_name(path.name + '-journal').write_bytes(b'preserve interrupted journal')
    connect, attempted = sqlite3.connect, []
    def watched(database, *args, **kwargs):
        if 'mode=rw' in str(database):
            attempted.append(True)
        return connect(database, *args, **kwargs)
    monkeypatch.setattr(sqlite3, 'connect', watched)
    before = path.read_bytes() if path.exists() else None
    gc.collect()
    descriptors = len(os.listdir('/proc/self/fd'))
    with pytest.raises(coordination.ScratchDatabaseReconciliationRequired):
        execute(session, manifest)
    assert attempted == [], 'opened unsupported SQLite read-write before rejecting it'
    assert source.exists() and not target.exists() and not session.journal_path.exists()
    assert (path.read_bytes() if path.exists() else None) == before
    assert len(os.listdir('/proc/self/fd')) <= descriptors - 1


def test_uncertain_commit_is_not_retried_even_when_values_persisted(case, monkeypatch):
    session, manifest, source, target, path = case
    commit = database_api._ScratchVideoDatabase._commit
    calls = []
    def lost_ack(db):
        commit(db)
        calls.append(True)
        raise OSError(errno.EIO, 'commit acknowledgement lost')
    monkeypatch.setattr(database_api._ScratchVideoDatabase, '_commit', lost_ack)
    with pytest.raises(coordination.ScratchDatabaseReconciliationRequired, match='acknowledgement lost'):
        execute(session, manifest)
    assert calls == [True] and target.exists() and not source.exists()
    with readonly_planner_session(path) as db:
        assert next(v for v in load_planner_context(db).videos if v.id == 100).source == manifest.payload.actions[0].target
    assert states(session, manifest) == ['PENDING', 'FS_APPLIED', 'FS_VERIFIED']
    proof = (path.read_bytes(), session.journal_path.read_bytes(), session.evidence_path.read_bytes())
    with pytest.raises(coordination.ScratchCoordinationStopped):
        execute(session, manifest)
    assert proof == (path.read_bytes(), session.journal_path.read_bytes(), session.evidence_path.read_bytes())
    assert calls == [True]


@pytest.mark.parametrize('state', ['DB_RECONCILED', 'COMPLETE'])
@pytest.mark.parametrize('when', ['before_write', 'after_fsync'])
def test_journal_ack_loss_preserves_bytes_and_never_returns_success(case, monkeypatch, state, when):
    session, manifest, source, target, path = case
    append, sync = journal.DurableJournal.append, os.fsync
    def failed_append(writer, entry):
        if when == 'before_write' and entry.state == state:
            raise OSError(errno.EIO, 'journal acknowledgement lost')
        append(writer, entry)
    def failed_sync(fd):
        sync(fd)
        if when == 'after_fsync' and os.readlink(f'/proc/self/fd/{fd}') == str(session.journal_path):
            raw = os.pread(fd, 65536, max(0, os.fstat(fd).st_size - 65536))
            if ('"state":"' + state + '"').encode() in raw.splitlines()[-1]:
                raise OSError(errno.EIO, 'journal fsync acknowledgement lost')
    monkeypatch.setattr(journal.DurableJournal, 'append', failed_append)
    monkeypatch.setattr(os, 'fsync', failed_sync)
    with pytest.raises(coordination.ScratchJournalReconciliationRequired):
        execute(session, manifest)
    assert target.exists() and not source.exists()
    entries = states(session, manifest)
    assert entries[:3] == ['PENDING', 'FS_APPLIED', 'FS_VERIFIED']
    assert ('COMPLETE' in entries) is (when == 'after_fsync' and state == 'COMPLETE')
    proof = (path.read_bytes(), session.evidence_path.read_bytes(), session.journal_path.read_bytes())
    with pytest.raises(coordination.ScratchCoordinationStopped):
        execute(session, manifest)
    assert proof == (path.read_bytes(), session.evidence_path.read_bytes(), session.journal_path.read_bytes())


def test_repeated_completed_action_never_runs_a_second_rename_or_db_update(case, monkeypatch):
    session, manifest, _, target, path = case
    rename, calls = fs._rename_noreplace, []
    def counted(*args):
        calls.append(True)
        rename(*args)
    monkeypatch.setattr(fs, '_rename_noreplace', counted)
    assert execute(session, manifest).outcome == 'COMPLETE'
    proof = (path.read_bytes(), session.journal_path.read_bytes(), session.evidence_path.read_bytes(), target.read_bytes())
    for method in (session.execute_video_move, session.execute):
        with pytest.raises(coordination.ScratchCoordinationStopped):
            method(manifest, manifest.payload.actions[0], action_index=0)
    assert calls == [True]
    assert proof == (path.read_bytes(), session.journal_path.read_bytes(), session.evidence_path.read_bytes(), target.read_bytes())


def test_target_collision_never_overwrites_file_or_db(case):
    session, manifest, source, target, path = case
    target.write_bytes(b'preserve occupied target')
    target.chmod(0o600)
    before = path.read_bytes()
    with pytest.raises(coordination.ScratchCoordinationStopped):
        execute(session, manifest)
    assert source.read_bytes() == b'original scratch video' and target.read_bytes() == b'preserve occupied target'
    assert path.read_bytes() == before and not session.journal_path.exists()


def test_live_db_reservation_blocks_an_unexpected_writer_before_rename(case, monkeypatch):
    session, manifest, _, _, path = case
    rename, checked = fs._rename_noreplace, []
    def checked_rename(*args):
        other = sqlite3.connect(path, timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                other.execute('BEGIN IMMEDIATE')
            checked.append(True)
        finally:
            other.close()
        rename(*args)
    monkeypatch.setattr(fs, '_rename_noreplace', checked_rename)
    assert execute(session, manifest).outcome == 'COMPLETE'
    assert checked == [True]


@pytest.mark.parametrize('when', ['before_db_reconciled', 'before_complete'])
def test_untrusted_journal_cannot_receive_db_ack_or_complete(case, monkeypatch, when):
    session, manifest, _, _, _ = case
    verify = database_api._ScratchVideoDatabase._verify_stored
    calls = []
    def corrupted(db):
        verify(db)
        calls.append(True)
        if len(calls) == (1 if when == 'before_db_reconciled' else 2):
            info = session.journal_path.stat()
            raw = session.journal_path.read_bytes()
            session.journal_path.write_bytes(raw[:-2] + b'X\n')
            os.utime(session.journal_path, ns=(info.st_atime_ns, info.st_mtime_ns))
    monkeypatch.setattr(database_api._ScratchVideoDatabase, '_verify_stored', corrupted)
    with pytest.raises(coordination.ScratchJournalReconciliationRequired):
        execute(session, manifest)
    raw = session.journal_path.read_bytes()
    assert b'"state":"COMPLETE"' not in raw
    if when == 'before_db_reconciled':
        assert b'"state":"DB_RECONCILED"' not in raw


@pytest.mark.parametrize('fault', ['none', 'apply', 'commit', 'verify'])
def test_success_and_failure_release_owned_db_and_evidence_descriptors(tmp_path, monkeypatch, fault):
    with ScratchActionSession.create(tmp_path) as session:
        manifest, _, _, _ = prepare_database(session)
        gc.collect()
        before = len(os.listdir('/proc/self/fd'))
        if fault != 'none':
            def failed(*args):
                raise OSError(errno.EIO, 'descriptor cleanup test')
            monkeypatch.setattr(database_api._ScratchVideoDatabase,
                {'apply': '_apply', 'commit': '_commit', 'verify': '_verify_stored'}[fault], failed)
            with pytest.raises(coordination.ScratchDatabaseReconciliationRequired):
                execute(session, manifest)
        else:
            execute(session, manifest)
        gc.collect()
        assert len(os.listdir('/proc/self/fd')) <= before - 1
        assert session._closed and session._database._closed
