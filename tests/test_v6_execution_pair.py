"""E6A: exactly two synthetic moves; no production data or recovery authority."""
from dataclasses import replace
import errno
import gc
import importlib
import importlib.util
import os
import sqlite3
from contextlib import closing

import pytest

from app import target_execution_database as database_api
from app import target_execution_filesystem as fs
from app import target_execution_identity as identity
from app import target_execution_journal as journal
from app import target_execution_coordination as coordination
from app.models import Video
from app.target_execution_manifest import build_execution_manifest, LocatorPatch, manifest_to_json
from app.target_planner_filesystem import inventory_filesystem, plan_library
from app.target_planner_service import load_planner_context, readonly_planner_session
from app.tools.target_plan import database_fingerprint
from test_v6_execution_filesystem import seal, changed_action
from v6_database_case import prepare_database, database_rows, legacy_v1


STATES = ['PENDING', 'FS_APPLIED', 'FS_VERIFIED', 'DB_RECONCILED', 'COMPLETE']


def api():
    assert importlib.util.find_spec('app.target_execution_pair') is not None, 'E6A pair owner is missing'
    return importlib.import_module('app.target_execution_pair')


def prepare_pair(owner, windows_root=None):
    _, source_a, target_a, path = prepare_database(owner)
    with closing(sqlite3.connect(path)) as db:
        current = db.execute('SELECT relative_path FROM videos WHERE id=200').fetchone()[0]
        target_b = owner.root / current
        source_b = owner.root / 'old/z-second.mkv'
        target_b.rename(source_b)
        db.execute("UPDATE videos SET relative_path='old/z-second.mkv',root_folder='old' WHERE id=200")
        db.commit()
    with readonly_planner_session(path) as db:
        context = load_planner_context(db)
    snapshot = inventory_filesystem(owner.root)
    manifest = build_execution_manifest(context, snapshot, plan_library(context, snapshot, windows_root=windows_root),
        database_fingerprint(path), windows_root=windows_root,
        created_at='2026-10-16T12:00:00Z', mount_identity=owner.mount_identity)
    assert not manifest.payload.generation_diagnostics
    actions = tuple(a for a in manifest.payload.actions if a.action == 'MOVE')
    assert [a.object_id for a in actions] == [100, 200]
    actions = (actions[0], replace(actions[1], depends_on=(actions[0].action_id,)))
    return seal(manifest, actions), (source_a, source_b), (target_a, target_b), path


@pytest.fixture
def case(tmp_path):
    with api()._ScratchVideoMovePair.create(tmp_path) as owner:
        yield owner, *prepare_pair(owner)


def history(owner, manifest):
    if not owner.journal_path.exists():
        return []
    return [(e.action_index, e.state) for e in journal.load_journal(owner.journal_path, manifest).entries]


def project(rows, manifest, count):
    columns = list(Video.__table__.columns.keys())
    expected = []
    ids = {a.object_id for a in manifest.payload.actions[:count]}
    for row in rows['videos']:
        values = list(row)
        for patch in manifest.payload.locator_patches:
            if patch.object_id in ids and row[columns.index('id')] == patch.object_id:
                assert values[columns.index(patch.field)] == patch.before
                values[columns.index(patch.field)] = patch.after
        expected.append(tuple(values))
    return {**rows, 'videos': tuple(expected)}


def change_physical(source, target, mutation):
    info = target.stat()
    if mutation == 'mtime':
        os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns + 1))
    elif mutation == 'inode':
        original = target.with_suffix('.original')
        target.rename(original)
        target.write_bytes(original.read_bytes())
        target.chmod(0o600)
        os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))
    elif mutation == 'hardlink':
        os.link(target, target.with_suffix('.alias'))
    else:
        assert mutation == 'source_reappears'
        source.write_bytes(b'unexpected source')


def assert_no_case_descriptors(owner):
    for fd in os.listdir('/proc/self/fd'):
        try:
            locator = os.readlink(f'/proc/self/fd/{fd}')
        except FileNotFoundError:
            continue
        assert locator != str(owner.root.parent)
        assert not locator.startswith(str(owner.root.parent) + '/')


def test_two_moves_have_one_writer_independent_evidence_and_exact_r1_r2(tmp_path, request, monkeypatch):
    owner = api()._ScratchVideoMovePair.create(tmp_path)
    request.addfinalizer(owner.close)
    manifest, sources, targets, path = prepare_pair(owner)
    original, immutable = database_rows(path), manifest_to_json(manifest)
    inodes = [p.stat().st_ino for p in sources]
    writers, captured, hash_calls = [], [], []
    create, append = journal.DurableJournal.create, journal.DurableJournal.append
    capture, post_hash = identity.ScratchIdentitySession.capture, database_api.post_state_hash_from_manifest

    def created(cls, *args, **kwargs):
        writer = create(*args, **kwargs)
        writers.append(writer)
        return writer

    def evidence(session, *args, **kwargs):
        record = capture(session, *args, **kwargs)
        captured.append((session.evidence_path, record))
        return record

    def hashed(*args):
        assert database_rows(path) == project(original, manifest, 2)
        hash_calls.append(True)
        return post_hash(*args)

    def appended(writer, entry):
        assert writer is writers[0]
        if entry.action_index == 1 and entry.state == 'PENDING':
            assert [(e.action_index, e.state) for e in writer.inspect_live(manifest)] == [(0, s) for s in STATES]
            assert database_rows(path) == project(original, manifest, 1)
            assert targets[0].stat().st_ino == inodes[0] and sources[1].exists()
            assert hash_calls == []
        if entry.state in {'DB_RECONCILED', 'COMPLETE'}:
            assert database_rows(path) == project(original, manifest, entry.action_index + 1)
        append(writer, entry)

    monkeypatch.setattr(journal.DurableJournal, 'create', classmethod(created))
    monkeypatch.setattr(journal.DurableJournal, 'append', appended)
    monkeypatch.setattr(identity.ScratchIdentitySession, 'capture', evidence)
    monkeypatch.setattr(database_api, 'post_state_hash_from_manifest', hashed)
    result = owner.execute(manifest)
    assert [r.outcome for r in result] == ['COMPLETE', 'COMPLETE']
    assert [r.action_index for r in result] == [0, 1]
    assert len(writers) == 1 and writers[0]._closed
    assert history(owner, manifest) == [(i, state) for i in (0, 1) for state in STATES]
    assert database_rows(path) == project(original, manifest, 2) and hash_calls
    assert all(not p.exists() for p in sources)
    assert [p.stat().st_ino for p in targets] == inodes
    assert len(captured) == 2 and captured[0][0] != captured[1][0]
    assert [record.action_index for _, record in captured] == [0, 1]
    for index, (evidence_path, record) in enumerate(captured):
        assert evidence_path.exists() and record.action == manifest.payload.actions[index]
        assert record.filesystem.st_ino == inodes[index]
        assert record.binding.root == str(owner.root)
    assert manifest_to_json(manifest) == immutable
    before = owner.journal_path.read_bytes(), path.read_bytes()
    with pytest.raises(coordination.ScratchCoordinationStopped):
        owner.execute(manifest)
    assert (owner.journal_path.read_bytes(), path.read_bytes()) == before


def test_historical_v1_stops_before_db_evidence_journal_or_rename(case, monkeypatch):
    owner, manifest, sources, targets, path = case
    legacy = legacy_v1(manifest)
    before, rows = path.read_bytes(), database_rows(path)
    calls = []
    open_db, capture = database_api._ScratchVideoDatabase._open, identity.ScratchIdentitySession.capture
    create, rename = journal.DurableJournal.create, fs._rename_noreplace
    monkeypatch.setattr(database_api._ScratchVideoDatabase, '_open', lambda db: calls.append('db') or open_db(db))
    monkeypatch.setattr(identity.ScratchIdentitySession, 'capture',
        lambda session, *args, **kwargs: calls.append('evidence') or capture(session, *args, **kwargs))
    monkeypatch.setattr(journal.DurableJournal, 'create',
        classmethod(lambda cls, *args, **kwargs: calls.append('journal') or create(*args, **kwargs)))
    monkeypatch.setattr(fs, '_rename_noreplace', lambda *args: calls.append('rename') or rename(*args))
    with pytest.raises(coordination.ScratchCoordinationStopped) as error:
        owner.execute(legacy)
    assert calls == []
    assert all(p.exists() for p in sources) and not any(p.exists() for p in targets)
    assert path.read_bytes() == before and database_rows(path) == rows
    assert not owner.journal_path.exists() and not any(p.exists() for p in owner.evidence_paths)
    assert type(error.value) is coordination.ScratchCoordinationStopped
    assert error.value.phase == 'VALIDATION' and not error.value.reconciliation_required
    assert 'legacy manifest has no sealed Windows root' in str(error.value.__cause__)


def test_bound_v2_root_reaches_e6a_final_verification(tmp_path):
    with api()._ScratchVideoMovePair.create(tmp_path) as owner:
        manifest, sources, targets, path = prepare_pair(owner, windows_root=r'\\server\Anime')
        assert manifest.payload.version == 2 and manifest.payload.windows_root == r'\\server\Anime'
        original = database_rows(path)
        result = owner.execute(manifest)
        assert [r.outcome for r in result] == ['COMPLETE', 'COMPLETE']
        assert history(owner, manifest) == [(i, state) for i in (0, 1) for state in STATES]
        assert database_rows(path) == project(original, manifest, 2)
        assert not any(p.exists() for p in sources) and all(p.exists() for p in targets)


@pytest.mark.parametrize('change', ['missing_dependency', 'reverse_dependency', 'duplicate_dependency', 'one', 'three', 'low', 'same_id'])
def test_only_exact_sealed_pair_is_accepted(case, change):
    owner, manifest, sources, targets, path = case
    a, b = manifest.payload.actions
    if change == 'missing_dependency': b = replace(b, depends_on=())
    elif change == 'reverse_dependency': a = replace(a, depends_on=(b.action_id,))
    elif change == 'duplicate_dependency': b = replace(b, depends_on=(a.action_id, a.action_id))
    elif change == 'low': b = replace(b, criticality='LOW', failure_policy='CONTINUE_IF_SAFE')
    elif change == 'same_id':
        b = changed_action(seal(manifest, (b,)), object_id=a.object_id)
    actions = (a,) if change == 'one' else (a, b)
    if change == 'three':
        third = changed_action(manifest, object_id=300, source='old/zz-third.mkv', target='Show/third.mkv')
        actions += (third,)
    manifest = seal(manifest, actions)
    before = path.read_bytes()
    with pytest.raises(coordination.ScratchCoordinationStopped): owner.execute(manifest)
    assert all(p.exists() for p in sources) and all(not p.exists() for p in targets)
    assert path.read_bytes() == before and not owner.journal_path.exists()


@pytest.mark.parametrize('fault', ['missing', 'foreign', 'wrong_before', 'wrong_after', 'filename'])
def test_foreign_or_incomplete_patches_stop_before_any_move(case, fault):
    owner, manifest, sources, targets, path = case
    patches = manifest.payload.locator_patches
    if fault == 'missing': patches = patches[:-1]
    elif fault == 'foreign': patches += (LocatorPatch('title', 1, 'relative_root_path', 'old', 'Show'),)
    elif fault == 'filename':
        patches += (LocatorPatch('video', 100, 'filename', 'old.mkv', 'new.mkv'),)
    else:
        patches = (replace(patches[0], **{'before' if fault == 'wrong_before' else 'after': 'wrong'}), *patches[1:])
    manifest = seal(manifest, locator_patches=patches)
    before = path.read_bytes()
    with pytest.raises(coordination.ScratchCoordinationStopped): owner.execute(manifest)
    assert path.read_bytes() == before and all(p.exists() for p in sources)
    assert not owner.journal_path.exists()


@pytest.mark.parametrize('index', [0, 1])
def test_collision_never_overwrites_target(case, index):
    owner, manifest, sources, targets, path = case
    targets[index].write_bytes(b'foreign file')
    before = database_rows(path)
    with pytest.raises(coordination.ScratchCoordinationStopped): owner.execute(manifest)
    assert targets[index].read_bytes() == b'foreign file' and sources[index].exists()
    assert database_rows(path) == project(before, manifest, index)
    assert history(owner, manifest) == ([(0, s) for s in STATES] if index else [])


@pytest.mark.parametrize('index', [0, 1])
@pytest.mark.parametrize('uncertain', [False, True])
def test_rename_failure_preserves_completed_predecessor_and_stops(case, monkeypatch, index, uncertain):
    owner, manifest, sources, targets, path = case
    original, rename, calls = database_rows(path), fs._rename_noreplace, []
    def failed(*args):
        current = len(calls)
        calls.append(current)
        if current == index:
            if uncertain: rename(*args)
            raise OSError(errno.EIO if uncertain else errno.EACCES, 'synthetic rename failure')
        rename(*args)
    monkeypatch.setattr(fs, '_rename_noreplace', failed)
    with pytest.raises(coordination.ScratchCoordinationStopped) as error: owner.execute(manifest)
    assert error.value.action_index == index and error.value.phase == 'RENAME'
    assert database_rows(path) == project(original, manifest, index)
    assert history(owner, manifest) == [(0, s) for s in STATES] * index + [(index, 'PENDING')]
    assert targets[index].exists() is uncertain and sources[index].exists() is not uncertain
    assert all(p.exists() for p in owner.evidence_paths[:index + 1])
    saved = owner.journal_path.read_bytes()
    with pytest.raises(coordination.ScratchCoordinationStopped): owner.execute(manifest)
    assert len(calls) == index + 1 and owner.journal_path.read_bytes() == saved


@pytest.mark.parametrize('index', [0, 1])
@pytest.mark.parametrize('after_append', [False, True])
@pytest.mark.parametrize('state', STATES)
def test_journal_failure_retains_actual_partial_state_without_retry(case, monkeypatch, index, after_append, state):
    owner, manifest, sources, targets, path = case
    original, append = database_rows(path), journal.DurableJournal.append
    def failed(writer, entry):
        if (entry.action_index, entry.state) == (index, state):
            if after_append: append(writer, entry)
            raise OSError(errno.EIO, 'synthetic append acknowledgement failure')
        append(writer, entry)
    monkeypatch.setattr(journal.DurableJournal, 'append', failed)
    with pytest.raises(coordination.ScratchJournalReconciliationRequired) as error: owner.execute(manifest)
    assert error.value.action_index == index and error.value.phase == state
    count = STATES.index(state)
    expected = [(0, s) for s in STATES] * index + [(index, s) for s in STATES[:count + int(after_append)]]
    assert history(owner, manifest) == expected
    committed = state in {'DB_RECONCILED', 'COMPLETE'}
    assert database_rows(path) == project(original, manifest, index + int(committed))
    assert sources[index].exists() is (state == 'PENDING')
    assert sources[1].exists() if index == 0 else targets[0].exists()
    before = owner.journal_path.read_bytes(), path.read_bytes()
    with pytest.raises(coordination.ScratchCoordinationStopped): owner.execute(manifest)
    assert (owner.journal_path.read_bytes(), path.read_bytes()) == before


@pytest.mark.parametrize('index', [0, 1])
def test_uncertain_db_commit_stops_without_repeating_or_reverting(case, monkeypatch, index):
    owner, manifest, sources, targets, path = case
    original, calls = database_rows(path), []
    commit = api()._ScratchPairDatabase._commit
    def failed(db, action_index):
        calls.append(action_index)
        commit(db, action_index)
        if action_index == index: raise OSError(errno.EIO, 'commit acknowledgement lost')
    monkeypatch.setattr(api()._ScratchPairDatabase, '_commit', failed)
    with pytest.raises(coordination.ScratchDatabaseReconciliationRequired) as error: owner.execute(manifest)
    assert error.value.phase == 'DB_COMMIT' and error.value.action_index == index
    assert calls == list(range(index + 1))
    assert database_rows(path) == project(original, manifest, index + 1)
    assert history(owner, manifest) == [(0, s) for s in STATES] * index + [(index, s) for s in STATES[:3]]
    assert targets[index].exists() and not sources[index].exists()


def test_wrong_action_index_binding_cannot_start_b(case, monkeypatch):
    owner, manifest, sources, targets, path = case
    capture = identity.ScratchIdentitySession.capture
    def wrong(session, sealed, action, *, action_index):
        return capture(session, sealed, action, action_index=0 if action_index == 1 else action_index)
    monkeypatch.setattr(identity.ScratchIdentitySession, 'capture', wrong)
    with pytest.raises(coordination.ScratchCoordinationStopped): owner.execute(manifest)
    assert history(owner, manifest) == [(0, s) for s in STATES]
    assert targets[0].exists() and sources[1].exists() and not owner.evidence_paths[1].exists()


def test_in_memory_completion_flag_cannot_authorize_either_action(case):
    owner, manifest, sources, targets, path = case
    owner.completed = True
    for action in manifest.payload.actions:
        with pytest.raises(fs.RenameRejected):
            with owner._filesystem.observe(manifest, action):
                pytest.fail('observation escaped exclusive execution owner')
    assert all(p.exists() for p in sources) and not owner.journal_path.exists()


@pytest.mark.parametrize('index', [0, 1])
@pytest.mark.parametrize('asset', ['identity', 'target'])
def test_uncertain_fsync_preserves_artifacts_and_stops(case, monkeypatch, index, asset):
    owner, manifest, sources, targets, path = case
    original, sync = database_rows(path), os.fsync
    watched = owner.evidence_paths[index] if asset == 'identity' else targets[index]
    def failed(fd):
        if os.readlink(f'/proc/self/fd/{fd}') == str(watched):
            raise OSError(errno.EIO, 'synthetic fsync uncertainty')
        sync(fd)
    monkeypatch.setattr(os, 'fsync', failed)
    with pytest.raises(coordination.ScratchFilesystemReconciliationRequired) as error: owner.execute(manifest)
    assert error.value.action_index == index
    assert error.value.phase == ('EVIDENCE_CAPTURE' if asset == 'identity' else 'RENAME')
    assert database_rows(path) == project(original, manifest, index)
    expected = [(0, state) for state in STATES] * index
    if asset == 'target': expected += [(index, 'PENDING')]
    assert history(owner, manifest) == expected
    assert owner.evidence_paths[index].exists()
    assert sources[index].exists() is (asset == 'identity')


@pytest.mark.parametrize('index', [0, 1])
@pytest.mark.parametrize('mutation', ['mtime', 'inode', 'hardlink', 'source_reappears'])
def test_physical_change_after_fs_applied_requires_reconciliation(case, monkeypatch, index, mutation):
    owner, manifest, sources, targets, path = case
    original = database_rows(path)
    append, rename = journal.DurableJournal.append, fs._rename_noreplace
    calls, evidence = [], {}
    def renamed(*args):
        calls.append(True)
        return rename(*args)
    def changed(writer, entry):
        append(writer, entry)
        if (entry.action_index, entry.state) == (index, 'FS_APPLIED'):
            evidence.update((p, p.read_bytes()) for p in owner.evidence_paths[:index + 1])
            change_physical(sources[index], targets[index], mutation)
    monkeypatch.setattr(fs, '_rename_noreplace', renamed)
    monkeypatch.setattr(journal.DurableJournal, 'append', changed)
    with pytest.raises(coordination.ScratchFilesystemReconciliationRequired) as error:
        owner.execute(manifest)
    assert error.value.reconciliation_required is True
    assert error.value.action_index == index and error.value.phase == 'VERIFICATION'
    assert isinstance(error.value.__cause__, fs.RenameRejected)
    assert error.value.__cause__.__traceback__ is not None
    assert history(owner, manifest) == [(0, s) for s in STATES] * index + [(index, s) for s in STATES[:2]]
    assert database_rows(path) == project(original, manifest, index)
    assert all(p.read_bytes() == raw for p, raw in evidence.items())
    assert targets[index].exists()
    assert sources[index].exists() is (mutation == 'source_reappears')
    if index == 0:
        assert sources[1].exists() and not targets[1].exists() and not owner.evidence_paths[1].exists()
    saved = owner.journal_path.read_bytes(), path.read_bytes()
    with pytest.raises(coordination.ScratchFilesystemReconciliationRequired) as repeated:
        owner.execute(manifest)
    assert repeated.value.phase == 'VERIFICATION' and repeated.value.action_index == index
    assert len(calls) == index + 1
    assert (owner.journal_path.read_bytes(), path.read_bytes()) == saved
    assert_no_case_descriptors(owner)


@pytest.mark.parametrize('asset', ['file', 'evidence', 'database_check', 'mtime', 'inode', 'hardlink', 'source_reappears'])
def test_changed_a_proof_after_complete_cannot_start_b(case, monkeypatch, asset):
    owner, manifest, sources, targets, path = case
    original = database_rows(path)
    append = journal.DurableJournal.append
    def changed(writer, entry):
        append(writer, entry)
        if entry.action_index == 0 and entry.state == 'COMPLETE':
            if asset == 'file': targets[0].write_bytes(b'changed A')
            elif asset == 'evidence': owner.evidence_paths[0].write_bytes(b'changed evidence')
            elif asset == 'database_check':
                def unconfirmed(_index): raise ValueError('independent R1 check failed')
                monkeypatch.setattr(owner._database, '_verify_stored', unconfirmed)
            else: change_physical(sources[0], targets[0], asset)
    monkeypatch.setattr(journal.DurableJournal, 'append', changed)
    expected = (coordination.ScratchDatabaseReconciliationRequired if asset == 'database_check'
        else coordination.ScratchFilesystemReconciliationRequired)
    with pytest.raises(expected) as error: owner.execute(manifest)
    assert error.value.reconciliation_required is True and error.value.action_index == 0
    assert error.value.phase == ('DB_FINAL_VERIFICATION' if asset == 'database_check'
        else 'COMPLETE' if asset == 'evidence' else 'IDENTITY_VERIFICATION')
    assert error.value.__cause__ is not None
    assert history(owner, manifest) == [(0, state) for state in STATES]
    assert database_rows(path) == project(original, manifest, 1)
    assert targets[0].exists()
    assert sources[1].exists() and not targets[1].exists() and not owner.evidence_paths[1].exists()
    saved = owner.journal_path.read_bytes(), path.read_bytes()
    with pytest.raises(expected): owner.execute(manifest)
    assert (owner.journal_path.read_bytes(), path.read_bytes()) == saved
    assert_no_case_descriptors(owner)


@pytest.mark.parametrize('mutation', ['mtime', 'inode', 'hardlink', 'source_reappears'])
def test_changed_completed_a_at_dependency_gate_requires_filesystem_reconciliation(case, monkeypatch, mutation):
    owner, manifest, sources, targets, path = case
    original = database_rows(path)
    predecessor = owner._require_predecessor
    evidence = {}
    def changed():
        assert [(e.action_index, e.state) for e in owner._writer.inspect_live(manifest)] == [(0, state) for state in STATES]
        assert owner._authorized_b is None
        evidence[owner.evidence_paths[0]] = owner.evidence_paths[0].read_bytes()
        change_physical(sources[0], targets[0], mutation)
        predecessor()
    monkeypatch.setattr(owner, '_require_predecessor', changed)
    with pytest.raises(coordination.ScratchFilesystemReconciliationRequired) as error:
        owner.execute(manifest)
    assert error.value.reconciliation_required is True
    assert error.value.phase == 'PREDECESSOR_IDENTITY' and error.value.action_index == 1
    assert isinstance(error.value.__cause__, fs.RenameRejected)
    assert history(owner, manifest) == [(0, state) for state in STATES]
    assert database_rows(path) == project(original, manifest, 1)
    assert all(p.read_bytes() == raw for p, raw in evidence.items())
    assert targets[0].exists() and owner._authorized_b is None
    assert sources[1].exists() and not targets[1].exists() and not owner.evidence_paths[1].exists()
    saved = owner.journal_path.read_bytes(), path.read_bytes()
    with pytest.raises(coordination.ScratchFilesystemReconciliationRequired): owner.execute(manifest)
    assert (owner.journal_path.read_bytes(), path.read_bytes()) == saved
    assert_no_case_descriptors(owner)


@pytest.mark.parametrize('phase', ['VERIFICATION', 'PREDECESSOR_IDENTITY'])
def test_classified_physical_stop_preserves_original_exception(case, monkeypatch, phase):
    owner, manifest, sources, targets, path = case
    verify = fs.ScratchObservation.verify
    stop = coordination.ScratchFilesystemReconciliationRequired('STOP: already classified', phase)
    def stopped(observation):
        if owner._phase == phase:
            raise stop
        return verify(observation)
    monkeypatch.setattr(fs.ScratchObservation, 'verify', stopped)
    with pytest.raises(coordination.ScratchFilesystemReconciliationRequired) as error:
        owner.execute(manifest)
    assert error.value is stop and error.value.__cause__ is None
    assert stop.phase == phase and stop.reconciliation_required is True


def test_existing_b_evidence_is_never_overwritten(case):
    owner, manifest, sources, targets, path = case
    owner.evidence_paths[1].write_bytes(b'foreign evidence')
    with pytest.raises(coordination.ScratchCoordinationStopped): owner.execute(manifest)
    assert owner.evidence_paths[1].read_bytes() == b'foreign evidence'
    assert history(owner, manifest) == [(0, state) for state in STATES]
    assert targets[0].exists() and sources[1].exists()


def test_final_hash_mismatch_does_not_prevent_valid_r1_but_stops_r2(case):
    owner, manifest, sources, targets, path = case
    manifest = seal(manifest, expected_post_hash='0' * 64)
    original = database_rows(path)
    with pytest.raises(coordination.ScratchDatabaseReconciliationRequired) as error: owner.execute(manifest)
    assert error.value.action_index == 1 and error.value.phase == 'DB_VERIFICATION'
    assert history(owner, manifest) == [(0, state) for state in STATES] + [(1, state) for state in STATES[:3]]
    assert database_rows(path) == project(original, manifest, 2)
    assert all(p.exists() for p in targets)


def test_shared_db_reservation_blocks_external_writer_between_actions(case, monkeypatch):
    owner, manifest, sources, targets, path = case
    append, blocked = journal.DurableJournal.append, []
    def inspected(writer, entry):
        append(writer, entry)
        if entry.action_index == 0 and entry.state == 'COMPLETE':
            with closing(sqlite3.connect(path, timeout=0)) as foreign:
                with pytest.raises(sqlite3.OperationalError, match='locked'):
                    foreign.execute("UPDATE videos SET filename='foreign' WHERE id=200")
                blocked.append(True)
    monkeypatch.setattr(journal.DurableJournal, 'append', inspected)
    assert [r.outcome for r in owner.execute(manifest)] == ['COMPLETE', 'COMPLETE']
    assert blocked == [True]


def test_external_commit_in_reservation_gap_is_detected_and_never_rebased(case, monkeypatch):
    owner, manifest, sources, targets, path = case
    connect, changed = sqlite3.connect, []
    class InterleavedConnection(sqlite3.Connection):
        def commit(self):
            super().commit()
            if not changed:
                with closing(connect(path, timeout=0)) as foreign:
                    foreign.execute("UPDATE videos SET filename='external committed change' WHERE id=200")
                    foreign.commit()
                changed.append(True)
    def connected(*args, **kwargs):
        if '?mode=rw' in str(args[0]): kwargs['factory'] = InterleavedConnection
        return connect(*args, **kwargs)
    monkeypatch.setattr(sqlite3, 'connect', connected)
    with pytest.raises(coordination.ScratchDatabaseReconciliationRequired): owner.execute(manifest)
    assert changed == [True]
    assert history(owner, manifest) == [(0, state) for state in STATES[:3]]
    assert targets[0].exists() and sources[1].exists() and not owner.evidence_paths[1].exists()
    with closing(connect(path)) as fresh:
        assert fresh.execute('SELECT filename FROM videos WHERE id=200').fetchone()[0] == 'external committed change'


def test_execution_never_opens_production_paths_or_another_database(case, monkeypatch):
    owner, manifest, sources, targets, path = case
    open_absolute, connect = fs._open_absolute, sqlite3.connect
    opened = []
    def opened_directory(candidate):
        assert candidate.is_relative_to(owner.root.parent)
        opened.append(candidate)
        return open_absolute(candidate)
    def connected(database, *args, **kwargs):
        from urllib.parse import unquote
        assert unquote(str(database)).split('?', 1)[0] == 'file:' + str(path)
        return connect(database, *args, **kwargs)
    monkeypatch.setattr(fs, '_open_absolute', opened_directory)
    monkeypatch.setattr(sqlite3, 'connect', connected)
    owner.execute(manifest)
    assert opened


def test_unacknowledged_noop_complete_cannot_start_b(case, monkeypatch):
    owner, manifest, sources, targets, path = case
    append = journal.DurableJournal.append
    def omitted(writer, entry):
        if entry.action_index == 0 and entry.state == 'COMPLETE': return
        append(writer, entry)
    monkeypatch.setattr(journal.DurableJournal, 'append', omitted)
    with pytest.raises(coordination.ScratchJournalReconciliationRequired): owner.execute(manifest)
    assert history(owner, manifest) == [(0, state) for state in STATES[:4]]
    assert targets[0].exists() and sources[1].exists() and not owner.evidence_paths[1].exists()


def test_only_execution_thread_can_use_b_authorization(case, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    owner, manifest, sources, targets, path = case
    append, rejected = journal.DurableJournal.append, []
    def foreign_thread():
        with pytest.raises(fs.RenameRejected):
            owner._filesystem.rename(manifest, manifest.payload.actions[1])
        rejected.append(True)
    def observed(writer, entry):
        if entry.action_index == 1 and entry.state == 'PENDING':
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(foreign_thread).result(timeout=5)
        append(writer, entry)
    monkeypatch.setattr(journal.DurableJournal, 'append', observed)
    owner.execute(manifest)
    assert rejected == [True]


def test_context_exit_waits_for_the_active_execution_owner(case, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
    import threading
    owner, manifest, sources, targets, path = case
    entered, release, exiting = threading.Event(), threading.Event(), threading.Event()
    run = owner._run_move
    def paused(index):
        if index == 0:
            entered.set()
            assert release.wait(timeout=10)
        return run(index)
    def exit_context():
        exiting.set()
        owner.__exit__(None, None, None)
    monkeypatch.setattr(owner, '_run_move', paused)
    with ThreadPoolExecutor(max_workers=2) as pool:
        execution = pool.submit(owner.execute, manifest)
        assert entered.wait(timeout=10)
        cleanup = pool.submit(exit_context)
        assert exiting.wait(timeout=10)
        try:
            with pytest.raises(FutureTimeoutError): cleanup.result(timeout=0.1)
        finally:
            release.set()
        assert [result.outcome for result in execution.result(timeout=20)] == ['COMPLETE', 'COMPLETE']
        cleanup.result(timeout=20)


@pytest.mark.parametrize('method', ['close', '__exit__'])
def test_foreign_process_cannot_run_inherited_sqlite_cleanup(case, monkeypatch, method):
    owner, manifest, sources, targets, path = case
    closed = []
    # Any DB cleanup in an inherited process could abort its parent's transaction.
    owner._resources.callback(lambda: closed.append(True))
    with monkeypatch.context() as inherited:
        inherited.setattr(os, 'getpid', lambda: owner._pid + 1)
        with pytest.raises(coordination.ScratchCoordinationStopped) as error:
            if method == 'close': owner.close()
            else: owner.__exit__(None, None, None)
        assert error.value.phase == 'VALIDATION'
        assert closed == []
    owner.close()
    assert closed == [True]


@pytest.mark.parametrize('signal_type', [KeyboardInterrupt, SystemExit])
def test_interruption_of_b_retains_a_and_original_signal(case, monkeypatch, signal_type):
    owner, manifest, sources, targets, path = case
    rename, calls = fs._rename_noreplace, []
    signal = signal_type('synthetic interrupted B')
    def interrupted(*args):
        calls.append(True)
        if len(calls) == 2: raise signal
        rename(*args)
    monkeypatch.setattr(fs, '_rename_noreplace', interrupted)
    with pytest.raises(signal_type) as error: owner.execute(manifest)
    assert error.value is signal
    assert any('action_index=1; phase=RENAME' in note for note in signal.__notes__)
    assert history(owner, manifest) == [(0, state) for state in STATES] + [(1, 'PENDING')]
    assert targets[0].exists() and sources[1].exists()


def test_uncertain_cleanup_closes_every_resource_once_and_preserves_complete_history(case, monkeypatch):
    owner, manifest, sources, targets, path = case
    close, calls = api()._ScratchPairDatabase.close, []
    before = len(os.listdir('/proc/self/fd'))
    def unacknowledged(db):
        close(db)
        calls.append(True)
        raise OSError(errno.EIO, 'DB close acknowledgement lost')
    monkeypatch.setattr(api()._ScratchPairDatabase, 'close', unacknowledged)
    with pytest.raises(coordination.ScratchFilesystemReconciliationRequired) as error: owner.execute(manifest)
    assert error.value.phase == 'CLOSE'
    assert history(owner, manifest) == [(index, state) for index in (0, 1) for state in STATES]
    assert calls == [True] and all(p.exists() for p in targets)
    assert len(os.listdir('/proc/self/fd')) <= before - 1
    with pytest.raises(coordination.ScratchCoordinationStopped): owner.execute(manifest)
    assert calls == [True]


@pytest.mark.parametrize('fault', ['fingerprint', 'trigger', 'row', 'schema'])
def test_database_conflicts_do_not_acquire_new_authority(case, monkeypatch, fault):
    owner, manifest, sources, targets, path = case
    if fault in {'fingerprint', 'trigger'}:
        with closing(sqlite3.connect(path)) as db:
            if fault == 'fingerprint': db.execute("UPDATE videos SET filename='unexpected' WHERE id=200")
            else: db.execute("CREATE TRIGGER reject_b BEFORE UPDATE ON videos WHEN NEW.id=200 BEGIN SELECT RAISE(ABORT,'B rejected'); END")
            db.commit()
        if fault == 'trigger':
            manifest = seal(manifest, db_fingerprint=type(manifest.payload.db_fingerprint)(**database_fingerprint(path)))
    else:
        append = journal.DurableJournal.append
        def tampered(writer, entry):
            append(writer, entry)
            if entry.action_index == 0 and entry.state == 'COMPLETE':
                connection = owner._database._connection
                connection.execute("UPDATE videos SET filename='unexpected' WHERE id=200" if fault == 'row' else 'CREATE TABLE foreign_table(id INTEGER)')
        monkeypatch.setattr(journal.DurableJournal, 'append', tampered)
    with pytest.raises(coordination.ScratchCoordinationStopped): owner.execute(manifest)
    if fault == 'fingerprint':
        assert all(p.exists() for p in sources) and not owner.journal_path.exists()
    else:
        assert targets[0].exists()
        assert history(owner, manifest)[:5] == [(0, s) for s in STATES]
        assert (1, 'COMPLETE') not in history(owner, manifest)


@pytest.mark.parametrize('method', ['filesystem', 'identity', 'execute', 'execute_video_move'])
def test_public_single_action_apis_still_reject_pair_dependencies(tmp_path, method):
    with coordination.ScratchActionSession.create(tmp_path) as owner:
        manifest, sources, targets, path = prepare_pair(owner)
        b = manifest.payload.actions[1]
        with pytest.raises(ValueError):
            if method == 'filesystem':
                with owner._filesystem.observe(manifest, b): pytest.fail('public primitive authorized B')
            elif method == 'identity': owner._identity.capture(manifest, b, action_index=1)
            else: getattr(owner, method)(manifest, manifest.payload.actions[0], action_index=0)
        assert all(p.exists() for p in sources) and all(not p.exists() for p in targets)


@pytest.mark.parametrize('fault', [None, 'rename', 'commit', 'journal', 'capture'])
def test_pair_runs_do_not_leak_descriptors_or_emit_resource_warnings(tmp_path, recwarn, monkeypatch, fault):
    api()
    gc.collect()
    before = len(os.listdir('/proc/self/fd'))
    for _ in range(2):
        with api()._ScratchVideoMovePair.create(tmp_path) as owner:
            manifest, _, _, _ = prepare_pair(owner)
            with monkeypatch.context() as injected:
                if fault:
                    def failed(*args, **kwargs): raise OSError(errno.EIO, 'synthetic cleanup coverage')
                    target, method = {
                        'rename': (fs, '_rename_noreplace'),
                        'commit': (api()._ScratchPairDatabase, '_commit'),
                        'journal': (journal.DurableJournal, 'append'),
                        'capture': (identity.ScratchIdentitySession, 'capture'),
                    }[fault]
                    injected.setattr(target, method, failed)
                    with pytest.raises(coordination.ScratchCoordinationStopped): owner.execute(manifest)
                else:
                    owner.execute(manifest)
    gc.collect()
    assert len(os.listdir('/proc/self/fd')) == before
    assert not [warning for warning in recwarn if issubclass(warning.category, ResourceWarning)]
