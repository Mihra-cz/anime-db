"""E7A executes the complete synthetic manifest through one sequential owner."""
from contextlib import closing
from dataclasses import replace
import errno
import gc
import importlib
import os
from pathlib import Path
import sqlite3
import signal
import select
import subprocess
import sys

import pytest
from sqlalchemy.orm import Session

from app import target_execution_coordination as stops
from app import target_execution_database as database
from app import target_execution_filesystem as fs
from app import target_execution_identity as identity
from app import target_execution_journal as journal
from app.database import make_engine
from app.maintenance import ScratchMaintenance, MaintenanceBusy
from app.models import Video
from app.target_execution_manifest import build_execution_manifest, manifest_to_json
from app.target_planner_filesystem import inventory_filesystem, plan_library
from app.target_planner_service import load_planner_context, readonly_planner_session
from app.tools.target_plan import database_fingerprint
from test_physical_naming import NOW
from test_v6_execution_filesystem import seal
from test_v6_execution_pair import STATES, assert_no_case_descriptors
from v6_database_case import prepare_database, database_rows


def api():
    return importlib.import_module('app.target_execution_sequence')


def prepare_sequence(owner, count=3, dependencies='chain'):
    _, _, _, path = prepare_database(owner)
    engine = make_engine(f'sqlite:///{path}')
    try:
        with Session(engine) as db:
            first = db.get(Video, 100)
            for index in range(1, count):
                source = owner.root / f'old/video-{index:03d}.mkv'
                source.write_bytes(f'video {index}'.encode())
                source.chmod(0o600)
                info = source.stat()
                db.add(Video(id=300 + index, catalog_title_id=first.catalog_title_id,
                    catalog_collection_id=first.catalog_collection_id, relative_path=source.relative_to(owner.root).as_posix(),
                    root_folder='old', filename=f'release - {index+2:02d}.mkv', size=info.st_size,
                    mtime_ns=info.st_mtime_ns, file_type='episode', season_episode_number=index+2,
                    local_episode_number=index+2, content_type_manual='episode',
                    episode_number_manual_override=index+2, episode_number_verified_at=NOW,
                    manual_hardsub_cs=True, manual_hardsub_verified_at=NOW,
                    czsk_availability_manual='missing'))
            db.commit()
    finally:
        engine.dispose()
    with readonly_planner_session(path) as db:
        context = load_planner_context(db)
    snapshot = inventory_filesystem(owner.root)
    manifest = build_execution_manifest(context, snapshot, plan_library(context, snapshot), database_fingerprint(path),
        created_at='2026-10-17T12:00:00Z', mount_identity=owner.mount_identity)
    assert not manifest.payload.generation_diagnostics
    previous, actions = [], []
    for action in manifest.payload.actions:
        dependency = tuple(previous[-1:]) if dependencies == 'chain' else tuple(previous) if dependencies == 'all' else ()
        actions.append(replace(action, depends_on=dependency))
        previous.append(action.action_id)
    manifest = seal(manifest, tuple(actions))
    moves = [a for a in manifest.payload.actions if a.action == 'MOVE']
    assert len(moves) == count and any(a.action == 'KEEP' for a in manifest.payload.actions)
    return manifest, path


@pytest.fixture
def case(tmp_path):
    with api().ScratchManifestExecution.create(tmp_path) as owner:
        manifest, path = prepare_sequence(owner)
        gate = ScratchMaintenance(path, owner.root)
        # The legacy CLI fingerprint used by setup leaves a read connection
        # for GC. Clear setup resources before measuring execution ownership;
        # no collection runs inside the executor or its failure assertions.
        gc.collect()
        yield owner, manifest, path, gate


def run(case, manifest=None):
    owner, approved, _, gate = case
    with gate.maintenance() as lease:
        return owner.execute(manifest or approved, maintenance_owner=lease)


def project(original, actions, patches):
    columns = list(Video.__table__.columns)
    names = [column.name for column in columns]
    ids = {a.object_id for a in actions if a.action == 'MOVE'}
    rows = []
    for row in original['videos']:
        values = list(row)
        for patch in patches:
            if patch.object_id in ids and row[names.index('id')] == patch.object_id:
                assert row[names.index(patch.field)] == patch.before
                values[names.index(patch.field)] = patch.after
        rows.append(tuple(values))
    return {**original, 'videos': tuple(rows)}


@pytest.mark.parametrize(('count', 'dependencies'), [(3, 'chain'), (5, 'all'), (3, 'none')])
def test_complete_manifest_keeps_one_writer_and_exact_prefix_states(tmp_path, monkeypatch, count, dependencies):
    with api().ScratchManifestExecution.create(tmp_path) as owner:
        manifest, path = prepare_sequence(owner, count, dependencies)
        original, immutable = database_rows(path), manifest_to_json(manifest)
        states, writers, captures, hashes = [], [], [], []
        create, append = journal.DurableJournal.create, journal.DurableJournal.append
        capture, hashed = identity.ScratchIdentitySession.capture, database.post_state_hash_from_manifest
        def created(cls, *args, **kwargs):
            writer = create(*args, **kwargs)
            writers.append(writer)
            return writer
        def appended(writer, entry):
            assert writer is writers[0]
            prefix = entry.action_index + int(entry.state in {'DB_RECONCILED', 'COMPLETE'})
            assert database_rows(path) == project(original, manifest.payload.actions[:prefix], manifest.payload.locator_patches)
            if entry.state == 'PENDING':
                assert [(e.action_index, e.state) for e in writer.inspect_live(manifest)] == [
                    (i, s) for i in range(entry.action_index) for s in STATES]
            append(writer, entry)
            states.append((entry.action_index, entry.state))
        def captured(context, *args, **kwargs):
            record = capture(context, *args, **kwargs)
            captures.append((context.evidence_path, record))
            return record
        def post(*args):
            assert states[-1] == (len(manifest.payload.actions)-1, 'FS_VERIFIED') or states[-1][0] == len(manifest.payload.actions)-1
            assert database_rows(path) == project(original, manifest.payload.actions, manifest.payload.locator_patches)
            hashes.append(True)
            return hashed(*args)
        monkeypatch.setattr(journal.DurableJournal, 'create', classmethod(created))
        monkeypatch.setattr(journal.DurableJournal, 'append', appended)
        monkeypatch.setattr(identity.ScratchIdentitySession, 'capture', captured)
        monkeypatch.setattr(database, 'post_state_hash_from_manifest', post)
        result = run((owner, manifest, path, ScratchMaintenance(path, owner.root)))
        assert [r.action_index for r in result] == list(range(len(manifest.payload.actions)))
        assert all(r.outcome == 'COMPLETE' for r in result)
        assert states == [(i, s) for i in range(len(result)) for s in STATES]
        assert len(writers) == 1 and writers[0]._closed and hashes
        assert len(captures) == len(result) and len({p for p, _ in captures}) == len(result)
        assert [record.action_index for _, record in captures] == list(range(len(result)))
        for action in manifest.payload.actions:
            assert (owner.root / action.target).is_file()
            if action.action == 'MOVE':
                assert not (owner.root / action.source).exists()
        assert manifest_to_json(manifest) == immutable
        assert_no_case_descriptors(owner)


@pytest.mark.parametrize('fault', ['unknown_dependency', 'forward_dependency', 'self_dependency', 'duplicate_dependency',
    'low', 'quarantine', 'foreign_patch', 'missing_action', 'collision'])
def test_invalid_or_incomplete_plan_stops_before_evidence_or_journal(case, fault):
    owner, manifest, path, _ = case
    actions = list(manifest.payload.actions)
    if fault == 'unknown_dependency': actions[0] = replace(actions[0], depends_on=('0'*64,))
    elif fault == 'forward_dependency': actions[0] = replace(actions[0], depends_on=(actions[-1].action_id,))
    elif fault == 'self_dependency': actions[0] = replace(actions[0], depends_on=(actions[0].action_id,))
    elif fault == 'duplicate_dependency': actions[1] = replace(actions[1], depends_on=(actions[0].action_id,)*2)
    elif fault == 'low': actions[-1] = replace(actions[-1], criticality='LOW', failure_policy='CONTINUE_IF_SAFE')
    elif fault == 'quarantine':
        from app.target_execution_manifest import _digest
        a = actions[-1]
        a = replace(a, action='QUARANTINE', target='Duplicates/video.mkv')
        actions[-1] = replace(a, action_id=_digest((a.object_kind, a.object_id, a.action, a.source, a.target)))
    elif fault == 'missing_action': actions = actions[:-1]
    elif fault == 'foreign_patch':
        from app.target_execution_manifest import LocatorPatch
        manifest = seal(manifest, locator_patches=manifest.payload.locator_patches + (LocatorPatch('video', 200, 'filename', 'a', 'b'),))
    else:
        moving = [a for a in actions if a.action == 'MOVE']
        (owner.root / moving[1].target).write_bytes(b'collision')
    manifest = seal(manifest, tuple(actions))
    before = path.read_bytes()
    with pytest.raises(stops.ScratchCoordinationStopped): run(case, manifest)
    assert path.read_bytes() == before and not owner.journal_path.exists()
    assert not any(owner.root.parent.glob(owner.root.name + '.identity-*.json'))
    assert_no_case_descriptors(owner)


@pytest.mark.parametrize('fault', ['rename', 'commit', 'journal'])
@pytest.mark.parametrize('applied', [False, True])
def test_middle_failure_preserves_complete_prefix_and_stops_without_retry(case, monkeypatch, fault, applied):
    owner, manifest, path, gate = case
    original, calls = database_rows(path), []
    middle = next(i for i, a in enumerate(manifest.payload.actions) if a.action == 'MOVE' and a.object_id != 100)
    if fault == 'rename':
        rename = fs._rename_noreplace
        def failed(*args):
            calls.append(owner._active_index)
            if owner._active_index == middle:
                if applied: rename(*args)
                raise OSError(errno.EIO if applied else errno.EACCES, 'injected rename STOP')
            return rename(*args)
        monkeypatch.setattr(fs, '_rename_noreplace', failed)
    elif fault == 'commit':
        commit = database._ScratchSequenceDatabase._commit
        def failed(db, index):
            calls.append(index)
            if index == middle:
                if applied: commit(db, index)
                raise OSError('injected commit STOP')
            return commit(db, index)
        monkeypatch.setattr(database._ScratchSequenceDatabase, '_commit', failed)
    else:
        append = journal.DurableJournal.append
        def failed(writer, entry):
            calls.append(entry.action_index)
            if entry.action_index == middle and entry.state == 'COMPLETE':
                if applied: append(writer, entry)
                raise OSError('injected append STOP')
            return append(writer, entry)
        monkeypatch.setattr(journal.DurableJournal, 'append', failed)
    with pytest.raises(stops.ScratchCoordinationStopped) as error: run(case)
    assert error.value.action_index == middle
    committed = middle + int(fault == 'journal' or (fault == 'commit' and applied))
    assert database_rows(path) == project(original, manifest.payload.actions[:committed], manifest.payload.locator_patches)
    entries = journal.load_journal(owner.journal_path, manifest).entries
    assert [(e.action_index, e.state) for e in entries if e.action_index < middle] == [
        (i, s) for i in range(middle) for s in STATES]
    assert all(e.action_index <= middle for e in entries)
    assert not any((owner.root / a.target).exists() for a in manifest.payload.actions[middle+1:] if a.action == 'MOVE')
    saved, count = owner.journal_path.read_bytes(), len(calls)
    with gate.maintenance() as lease, pytest.raises(stops.ScratchCoordinationStopped):
        owner.execute(manifest, maintenance_owner=lease)
    assert owner.journal_path.read_bytes() == saved and len(calls) == count
    assert_no_case_descriptors(owner)


def test_missing_or_shared_maintenance_cannot_start(case):
    owner, manifest, path, gate = case
    before = path.read_bytes()
    with gate.writer() as lease, pytest.raises(stops.ScratchCoordinationStopped):
        owner.execute(manifest, maintenance_owner=lease)
    assert path.read_bytes() == before and not owner.journal_path.exists()
    assert_no_case_descriptors(owner)


def test_completed_keep_is_rechecked_before_later_move(case, monkeypatch):
    owner, manifest, path, _ = case
    kept = next(i for i, a in enumerate(manifest.payload.actions) if a.action == 'KEEP')
    assert kept < len(manifest.payload.actions)-1
    original, append = database_rows(path), journal.DurableJournal.append
    def changed(writer, entry):
        append(writer, entry)
        if entry.action_index == kept and entry.state == 'COMPLETE':
            (owner.root / manifest.payload.actions[kept].source).write_bytes(b'changed KEEP')
    monkeypatch.setattr(journal.DurableJournal, 'append', changed)
    with pytest.raises(stops.ScratchFilesystemReconciliationRequired): run(case)
    assert database_rows(path) == original
    assert all((owner.root / a.source).exists() for a in manifest.payload.actions if a.action == 'MOVE')


def test_keep_rejects_a_write_even_when_content_and_mtime_are_restored(case, monkeypatch):
    owner, manifest, path, _ = case
    kept = next(i for i, action in enumerate(manifest.payload.actions) if action.action == 'KEEP')
    append = journal.DurableJournal.append
    def changed(writer, entry):
        append(writer, entry)
        if entry.action_index == kept and entry.state == 'FS_APPLIED':
            target = owner.root / manifest.payload.actions[kept].source
            before, raw = target.stat(), target.read_bytes()
            target.write_bytes(raw)
            os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    monkeypatch.setattr(journal.DurableJournal, 'append', changed)
    with pytest.raises(stops.ScratchFilesystemReconciliationRequired): run(case)
    assert journal.load_journal(owner.journal_path, manifest).entries[-1].state == 'FS_APPLIED'


def test_success_and_failure_release_owned_fds(case):
    owner, manifest, path, gate = case
    gc.collect()
    before = len(os.listdir('/proc/self/fd'))
    run(case)
    assert_no_case_descriptors(owner)
    gc.collect()
    assert len(os.listdir('/proc/self/fd')) <= before


@pytest.mark.parametrize('fault', ['dependency', 'patch'])
def test_rejection_closes_owned_fds_with_gc_disabled_and_traceback_retained(case, fault):
    owner, manifest, path, _ = case
    if fault == 'dependency':
        actions = list(manifest.payload.actions)
        actions[0] = replace(actions[0], depends_on=('0'*64,))
        manifest = seal(manifest, tuple(actions))
    else:
        from app.target_execution_manifest import LocatorPatch
        manifest = seal(manifest, locator_patches=manifest.payload.locator_patches +
            (LocatorPatch('video', 200, 'filename', 'a', 'b'),))
    enabled = gc.isenabled()
    gc.disable()
    try:
        with pytest.raises(stops.ScratchCoordinationStopped) as error:
            run(case, manifest)
        assert error.value.__traceback__ is not None
        assert_no_case_descriptors(owner)
    finally:
        if enabled:
            gc.enable()


@pytest.mark.parametrize('fault', ['missing', 'closed', 'foreign'])
def test_only_the_live_matching_exclusive_owner_can_authorize(case, fault):
    owner, manifest, path, gate = case
    lease = None
    if fault == 'closed':
        with gate.maintenance() as lease:
            pass
    elif fault == 'foreign':
        gate = ScratchMaintenance(path.with_name('foreign.sqlite'), owner.root)
    if fault == 'foreign':
        with gate.maintenance() as lease, pytest.raises(stops.ScratchCoordinationStopped):
            owner.execute(manifest, maintenance_owner=lease)
    else:
        with pytest.raises(stops.ScratchCoordinationStopped):
            owner.execute(manifest, maintenance_owner=lease)
    assert not owner.journal_path.exists() and not owner.evidence_paths
    assert_no_case_descriptors(owner)


def test_a_separate_process_cannot_write_while_the_sequence_owns_maintenance(case, monkeypatch):
    owner, manifest, path, gate = case
    append, checked = journal.DurableJournal.append, []
    def appended(writer, entry):
        append(writer, entry)
        if entry.action_index == 0 and entry.state == 'COMPLETE':
            code = '''from pathlib import Path
import sys
from app.maintenance import ScratchMaintenance, MaintenanceBusy
gate = ScratchMaintenance(Path(sys.argv[1]), Path(sys.argv[2]))
for scope in (gate.writer, gate.maintenance):
    try:
        with scope():
            raise AssertionError('execution lost exclusive ownership')
    except MaintenanceBusy:
        pass
print('assertions_completed')
'''
            child = subprocess.run([sys.executable, '-c', code, str(path), str(owner.root)],
                capture_output=True, text=True, timeout=15)
            assert child.returncode == 0 and child.stdout.strip() == 'assertions_completed' and not child.stderr
            checked.append(True)
    monkeypatch.setattr(journal.DurableJournal, 'append', appended)
    run(case)
    assert checked == [True]
    with gate.writer():
        pass


def test_inherited_owner_cannot_execute_or_close_parent_sqlite(case):
    owner, manifest, path, gate = case
    with gate.maintenance() as lease:
        reader, writer = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(reader)
            try:
                for operation in (lambda: owner.execute(manifest, maintenance_owner=lease), owner.close):
                    try:
                        operation()
                    except stops.ScratchCoordinationStopped:
                        pass
                    else:
                        raise AssertionError('child inherited execution authority')
                os.write(writer, b'assertions_completed')
                os._exit(0)
            except BaseException:
                os._exit(1)
        os.close(writer)
        reaped = False
        try:
            assert select.select([reader], [], [], 15)[0], 'forked child did not finish assertions'
            marker = os.read(reader, 64)
            _, status = os.waitpid(pid, 0)
            reaped = True
            assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
            assert marker == b'assertions_completed'
        finally:
            os.close(reader)
            if not reaped:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
        assert not owner.journal_path.exists()
        assert owner.execute(manifest, maintenance_owner=lease)


@pytest.mark.parametrize('fault', ['journal', 'rename', 'commit'])
def test_interruption_preserves_complete_prefix_and_releases_all_owned_fds(case, monkeypatch, fault):
    owner, manifest, path, gate = case
    middle = next(i for i, action in enumerate(manifest.payload.actions) if action.action == 'MOVE')
    target, name = {'journal': (journal.DurableJournal, 'append'),
        'rename': (fs, '_rename_noreplace'), 'commit': (database._ScratchSequenceDatabase, '_commit')}[fault]
    original = getattr(target, name)
    def interrupted(*args, **kwargs):
        result = original(*args, **kwargs)
        if owner._active_index == middle:
            raise KeyboardInterrupt('synthetic sequence interruption')
        return result
    monkeypatch.setattr(target, name, interrupted)
    with pytest.raises(KeyboardInterrupt): run(case)
    entries = journal.load_journal(owner.journal_path, manifest).entries
    assert [(e.action_index, e.state) for e in entries if e.action_index < middle] == [
        (index, state) for index in range(middle) for state in STATES]
    assert all(entry.action_index <= middle for entry in entries)
    assert_no_case_descriptors(owner)
    before = path.read_bytes(), owner.journal_path.read_bytes()
    with gate.maintenance() as lease, pytest.raises(stops.ScratchCoordinationStopped):
        owner.execute(manifest, maintenance_owner=lease)
    assert (path.read_bytes(), owner.journal_path.read_bytes()) == before


def test_final_hash_is_not_checked_at_an_intermediate_prefix(case):
    owner, manifest, path, _ = case
    manifest = seal(manifest, expected_post_hash='0'*64)
    with pytest.raises(stops.ScratchDatabaseReconciliationRequired) as error: run(case, manifest)
    assert error.value.action_index == len(manifest.payload.actions)-1
    entries = journal.load_journal(owner.journal_path, manifest).entries
    assert sum(e.state == 'COMPLETE' for e in entries) == len(manifest.payload.actions)-1
    assert entries[-1].state == 'FS_VERIFIED'


def test_missing_independent_read_after_a_middle_commit_is_a_stop(case, monkeypatch):
    owner, manifest, path, _ = case
    middle = next(index for index, action in enumerate(manifest.payload.actions) if action.action == 'MOVE')
    original, connect, checked = database_rows(path), sqlite3.connect, []
    def unavailable(database_url, *args, **kwargs):
        if str(database_url).endswith('?mode=ro') and owner._phase == 'DB_VERIFICATION' and owner._active_index == middle:
            checked.append(True)
            raise sqlite3.OperationalError('independent committed-state reader unavailable')
        return connect(database_url, *args, **kwargs)
    monkeypatch.setattr(sqlite3, 'connect', unavailable)
    with pytest.raises(stops.ScratchDatabaseReconciliationRequired) as error: run(case)
    monkeypatch.setattr(sqlite3, 'connect', connect)
    assert checked and error.value.phase == 'DB_VERIFICATION' and error.value.action_index == middle
    assert database_rows(path) == project(original, manifest.payload.actions[:middle+1], manifest.payload.locator_patches)
    entries = journal.load_journal(owner.journal_path, manifest).entries
    assert entries[-1].action_index == middle and entries[-1].state == 'FS_VERIFIED'
    assert_no_case_descriptors(owner)


def test_legacy_single_action_rollback_cannot_adopt_sequence_evidence(case):
    from app.target_execution_rollback import propose_scratch_video_rollback, ScratchRollbackStopped
    owner, manifest, path, _ = case
    run(case)
    original = path.read_bytes(), owner.journal_path.read_bytes()
    with pytest.raises(ScratchRollbackStopped):
        propose_scratch_video_rollback(manifest, manifest.payload.actions[0], action_index=0,
            scratch_root=owner.root, journal_path=owner.journal_path, evidence_path=owner.evidence_paths[0])
    assert (path.read_bytes(), owner.journal_path.read_bytes()) == original


def test_losing_exclusive_maintenance_after_rename_stops_before_db_update(case, monkeypatch):
    owner, manifest, path, gate = case
    original, append = database_rows(path), journal.DurableJournal.append
    index = next(i for i, action in enumerate(manifest.payload.actions) if action.action == 'MOVE')
    def released(writer, entry):
        append(writer, entry)
        if entry.action_index == index and entry.state == 'FS_APPLIED':
            owner._maintenance_owner.close()
    monkeypatch.setattr(journal.DurableJournal, 'append', released)
    with pytest.raises(stops.ScratchCoordinationStopped): run(case)
    assert database_rows(path) == original
    assert (owner.root / manifest.payload.actions[index].target).exists()
    assert not (owner.root / manifest.payload.actions[index].source).exists()
    assert journal.load_journal(owner.journal_path, manifest).entries[-1].state == 'FS_APPLIED'
    assert_no_case_descriptors(owner)
