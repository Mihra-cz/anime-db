"""E7H separately approves only empty original directories after the E7G gate."""
from dataclasses import replace
from contextlib import closing
import errno
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import threading

import pytest

from app import target_execution_coordination as stops
from app.maintenance import ScratchMaintenance
from app.target_execution_manifest import manifest_to_json, post_state_hash_from_manifest
from app.target_execution_sequence import ScratchManifestExecution
from app.target_planner_filesystem import inventory_filesystem
from app.target_planner_service import load_planner_context, readonly_planner_session
from test_v6_execution_file_actions import generate
from test_v6_execution_pair import STATES, assert_no_case_descriptors
from v6_database_case import database_rows, prepare_database


def cleanup_api():
    from app import target_execution_cleanup
    return target_execution_cleanup


def prepare(owner, *, mixed=False):
    _, _, _, path = prepare_database(owner)
    (owner.root / 'old/copy').mkdir(mode=0o700)
    (owner.root / 'old/OST').mkdir(mode=0o700)
    if mixed:
        (owner.root / 'old/source.mkv').rename(owner.root / 'old/copy/source.mkv')
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("UPDATE videos SET relative_path='old/copy/source.mkv' WHERE id=100")
        (owner.root / 'old/OST/audio.flac').write_bytes(b'auxiliary audio')
        (owner.root / 'old/OST/audio.flac').chmod(0o600)
    else:
        # Most fault cases need just one file action; the mixed success case
        # above exercises existing KEEP and auxiliary preservation.
        with closing(sqlite3.connect(path)) as db, db:
            for source, in db.execute('SELECT relative_path FROM external_subtitles'):
                (owner.root / source).unlink()
            for source, in db.execute('SELECT relative_path FROM videos WHERE id != 100'):
                (owner.root / source).unlink()
            db.execute('DELETE FROM external_subtitle_compatibilities')
            db.execute('DELETE FROM external_subtitles')
            db.execute('DELETE FROM videos WHERE id != 100')
    (owner.root / 'empty/unapproved').mkdir(parents=True, mode=0o700)
    (owner.root / 'empty').chmod(0o700)
    (owner.root / 'Subs').mkdir(exist_ok=True, mode=0o700)
    (owner.root / 'Subs/empty').mkdir(mode=0o700)
    return generate(owner, path), path


def run(owner, manifest, path, decision):
    with ScratchMaintenance(path, owner.root).maintenance() as lease:
        return owner.execute(manifest, maintenance_owner=lease, cleanup_decision=decision)


def records(owner):
    rows = [json.loads(line) for line in owner.cleanup_evidence_path.read_bytes().splitlines()]
    previous = None
    for i, row in enumerate(rows):
        assert row['sequence'] == i and row['previous_sha256'] == previous
        body = {k: v for k, v in row.items() if k != 'sha256'}
        raw = json.dumps(body, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode()
        assert row['sha256'] == hashlib.sha256(raw).hexdigest()
        previous = row['sha256']
    return rows


def test_only_explicit_empty_original_directories_removed_after_original_hash(tmp_path, monkeypatch):
    api = cleanup_api()
    from app import target_execution_database as database
    with ScratchManifestExecution.create(tmp_path) as owner:
        manifest, path = prepare(owner, mixed=True)
        immutable = manifest_to_json(manifest)
        before_files = {a.target: (owner.root / a.source).read_bytes() for a in manifest.payload.actions}
        observed = {}
        original_post_hash, hash_checks = database.post_state_hash_from_manifest, []
        def checked_hash(*args, **kwargs):
            assert (owner.root / 'old/copy').is_dir()
            hash_checks.append(True)
            return original_post_hash(*args, **kwargs)
        monkeypatch.setattr(database, 'post_state_hash_from_manifest', checked_hash)

        def approve(proposal):
            # The decision consumes a verified post-execution state, never R0.
            assert [(e.action_index, e.state) for e in owner._writer.inspect_live(manifest)] == [
                (i, s) for i in range(len(manifest.payload.actions)) for s in STATES]
            with readonly_planner_session(path) as session:
                context = load_planner_context(session)
            assert post_state_hash_from_manifest(manifest, context, inventory_filesystem(owner.root)) == manifest.payload.expected_post_hash
            observed.update(db=path.read_bytes(), rows=database_rows(path), journal=owner.journal_path.read_bytes(),
                proposal=proposal, directories=owner.directory_evidence_path.read_bytes())
            assert {'old', 'old/copy', 'old/OST', 'empty/unapproved'} <= {d.path for d in proposal.candidates}
            # E7C calls an otherwise unused parent containing only directories
            # REVIEW, not UNRESOLVED_EMPTY. Cleanup must retain that distinction.
            assert 'empty' not in {d.path for d in proposal.candidates}
            assert not any(d.path.startswith(('Show', 'Subs', 'Duplicates')) for d in proposal.candidates)
            return api.approve_cleanup(proposal, ('old', 'old/copy', 'old/OST'), actor='operator', reason='reviewed scratch cleanup')

        outcomes = run(owner, manifest, path, approve)
        assert hash_checks
        assert len(outcomes) == len(manifest.payload.actions)
        assert not (owner.root / 'old').exists()
        assert (owner.root / 'empty/unapproved').is_dir()
        assert (owner.root / 'Subs/empty').is_dir()
        assert all((owner.root / d).is_dir() for d in manifest.payload.directories)
        assert {a.target: (owner.root / a.target).read_bytes() for a in manifest.payload.actions} == before_files
        assert path.read_bytes() == observed['db'] and database_rows(path) == observed['rows']
        assert owner.journal_path.read_bytes() == observed['journal']
        assert owner.directory_evidence_path.read_bytes() == observed['directories']
        assert manifest_to_json(manifest) == immutable
        evidence = records(owner)
        assert [r['state'] for r in evidence] == ['BOUND', 'INTENT', 'VERIFIED', 'INTENT', 'VERIFIED', 'INTENT', 'VERIFIED', 'COMPLETE']
        assert [r['path'] for r in evidence if r['state'] == 'VERIFIED'] == ['old/OST', 'old/copy', 'old']
        result = owner.cleanup_outcome
        assert result.removed == ('old/OST', 'old/copy', 'old')
        assert result.before_snapshot == observed['proposal'].before_snapshot
        assert result.after_snapshot != result.before_snapshot
        assert result.after_snapshot_sha256 != manifest.payload.expected_post_hash
        assert 'empty/unapproved' in result.preserved
        assert_no_case_descriptors(owner)


def test_subset_approval_preserves_unapproved_parent_and_sibling(tmp_path):
    api = cleanup_api()
    with ScratchManifestExecution.create(tmp_path) as owner:
        manifest, path = prepare(owner)
        run(owner, manifest, path, lambda proposal: api.approve_cleanup(proposal,
            ('old/copy',), actor='operator', reason='only this leaf'))
        assert not (owner.root / 'old/copy').exists()
        assert (owner.root / 'old/OST').is_dir() and (owner.root / 'old').is_dir()
        assert owner.cleanup_outcome.removed == ('old/copy',)


def test_concurrent_rejected_execute_cannot_replace_live_cleanup_decision(tmp_path, monkeypatch):
    with ScratchManifestExecution.create(tmp_path) as owner:
        manifest, path = prepare(owner)
        entered, called, errors, threads = threading.Event(), [], [], []
        execute_once, run_move = owner._execute_once, owner._run_move
        def execute(*args, **kwargs):
            if threading.current_thread().name == 'foreign-execute':
                entered.set()
            return execute_once(*args, **kwargs)
        def foreign():
            try:
                owner.execute(manifest, cleanup_decision=lambda proposal: called.append('foreign'))
            except stops.ScratchCoordinationStopped as exc:
                errors.append(exc)
        def move(index):
            thread = threading.Thread(target=foreign, name='foreign-execute')
            threads.append(thread)
            thread.start()
            assert entered.wait(5)
            return run_move(index)
        monkeypatch.setattr(owner, '_execute_once', execute)
        monkeypatch.setattr(owner, '_run_move', move)
        run(owner, manifest, path, lambda proposal: called.append('approved caller'))
        for thread in threads:
            thread.join(5)
            assert not thread.is_alive()
        assert called == ['approved caller']
        assert len(errors) == 1


@pytest.mark.parametrize('tamper', ['hash', 'paths', 'actor', 'reason', 'type', 'foreign'])
def test_bad_or_foreign_approval_preserves_all_directories(tmp_path, tamper):
    api = cleanup_api()
    with ScratchManifestExecution.create(tmp_path) as owner:
        manifest, path = prepare(owner)
        def decision(proposal):
            approved = api.approve_cleanup(proposal, ('old/copy',), actor='operator', reason='reviewed')
            return {
                'hash': lambda: replace(approved, approval_sha256='0' * 64),
                'paths': lambda: replace(approved, directories=('Show',)),
                'actor': lambda: replace(approved, actor=''),
                'reason': lambda: replace(approved, reason=''),
                'type': lambda: asdict_approval(approved),
                'foreign': lambda: replace(approved, proposal_sha256='f' * 64),
            }[tamper]()
        with pytest.raises(stops.ScratchCoordinationStopped):
            run(owner, manifest, path, decision)
        assert (owner.root / 'old/copy').is_dir()
        assert not owner.cleanup_evidence_path.exists()


def asdict_approval(approval):
    from dataclasses import asdict
    return asdict(approval)


def test_genuine_approval_from_another_completed_execution_is_rejected(tmp_path):
    api, saved = cleanup_api(), []
    with ScratchManifestExecution.create(tmp_path) as first:
        manifest, path = prepare(first)
        def decide(proposal):
            approval = api.approve_cleanup(proposal, ('old/copy',), actor='operator', reason='first execution only')
            saved.append(approval)
            return approval
        run(first, manifest, path, decide)
    with ScratchManifestExecution.create(tmp_path) as second:
        manifest, path = prepare(second)
        with pytest.raises(stops.ScratchCoordinationStopped):
            run(second, manifest, path, lambda proposal: saved[0])
        assert (second.root / 'old/copy').is_dir()
        assert not second.cleanup_evidence_path.exists()


@pytest.mark.parametrize('artifact', ['existing', 'symlink'])
def test_cleanup_evidence_never_overwrites_an_existing_entry(tmp_path, artifact):
    api = cleanup_api()
    with ScratchManifestExecution.create(tmp_path) as owner:
        manifest, path = prepare(owner)
        def decision(proposal):
            if artifact == 'existing':
                owner.cleanup_evidence_path.write_bytes(b'foreign evidence')
            else:
                owner.cleanup_evidence_path.symlink_to(path)
            return api.approve_cleanup(proposal, ('old/copy',), actor='operator', reason='reviewed')
        with pytest.raises(stops.ScratchCoordinationStopped):
            run(owner, manifest, path, decision)
        assert (owner.root / 'old/copy').is_dir()
        if artifact == 'existing':
            assert owner.cleanup_evidence_path.read_bytes() == b'foreign evidence'
        else:
            assert owner.cleanup_evidence_path.is_symlink()


@pytest.mark.parametrize('change', ['unknown_file', 'unknown_directory', 'symlink', 'directory_identity',
    'root', 'mount', 'mount_id', 'maintenance', 'journal', 'db_bytes'])
def test_changed_approved_state_stops_before_rmdir(tmp_path, monkeypatch, change):
    api = cleanup_api()
    from app import target_execution_filesystem as fs
    with ScratchManifestExecution.create(tmp_path) as owner:
        manifest, path = prepare(owner)
        def decision(proposal):
            approval = api.approve_cleanup(proposal, ('old/copy',), actor='operator', reason='reviewed')
            leaf = owner.root / 'old/copy'
            if change == 'unknown_file':
                (leaf / 'unknown.txt').write_bytes(b'must survive')
            elif change == 'unknown_directory':
                (leaf / 'unknown').mkdir(mode=0o700)
            elif change in {'symlink', 'directory_identity'}:
                info = leaf.stat()
                leaf.rename(tmp_path / 'detached')
                if change == 'symlink':
                    leaf.symlink_to(tmp_path / 'detached', target_is_directory=True)
                else:
                    leaf.mkdir(mode=0o700)
                    os.utime(leaf, ns=(info.st_atime_ns, info.st_mtime_ns))
            elif change == 'root':
                owner.root.rename(tmp_path / 'detached-root')
                owner.root.mkdir(mode=0o700)
            elif change == 'mount':
                monkeypatch.setattr(fs, '_read_mounts', lambda: ())
            elif change == 'mount_id':
                mount_id, held = fs._mount_id, owner._directories.pins['old/copy']
                monkeypatch.setattr(fs, '_mount_id', lambda fd: -1 if fd == held else mount_id(fd))
            elif change == 'maintenance':
                owner._maintenance_owner = None
            elif change == 'journal':
                owner._history.pop()
            elif change == 'db_bytes':
                with path.open('ab') as stream:
                    stream.write(b'foreign write')
            return approval
        with pytest.raises(stops.ScratchCoordinationStopped):
            run(owner, manifest, path, decision)
        assert not owner.cleanup_evidence_path.exists()
        if change == 'unknown_file':
            assert (owner.root / 'old/copy/unknown.txt').read_bytes() == b'must survive'
        assert_no_case_descriptors(owner)


@pytest.mark.parametrize('failure', ['final_hash', 'incomplete_e1'])
def test_incomplete_execution_never_reaches_cleanup_decision(tmp_path, monkeypatch, failure):
    from app import target_execution_database as database
    with ScratchManifestExecution.create(tmp_path) as owner:
        manifest, path = prepare(owner)
        if failure == 'final_hash':
            def rejected(db):
                raise ValueError('original expected_post_hash did not pass')
            monkeypatch.setattr(database._ScratchSequenceDatabase, '_verify_post_hash', rejected)
        else:
            append = owner._append
            def appended(state):
                if state == 'COMPLETE':
                    raise OSError(errno.EIO, 'E1 COMPLETE not durable')
                return append(state)
            monkeypatch.setattr(owner, '_append', appended)
        called = []
        with pytest.raises(stops.ScratchCoordinationStopped):
            run(owner, manifest, path, lambda proposal: called.append(proposal))
        assert not called and not owner.cleanup_evidence_path.exists()
        assert (owner.root / 'old/copy').is_dir()


@pytest.mark.parametrize('moment', ['intent_write', 'torn_intent', 'intent_fsync', 'intent_parent_fsync',
    'rmdir', 'rmdir_after', 'false_success', 'renamed', 'parent_fsync', 'verified_write', 'complete_fsync'])
def test_failure_preserves_exact_prefix_files_and_db_without_retry(tmp_path, monkeypatch, moment):
    api = cleanup_api()
    with ScratchManifestExecution.create(tmp_path) as owner:
        manifest, path = prepare(owner)
        real_write, real_fsync, real_rmdir = os.write, os.fsync, os.rmdir
        state, observed = {}, {}
        append = api._Cleanup._append

        def appended(cleanup, current, **values):
            state['current'] = current
            state['cleanup'] = cleanup
            return append(cleanup, current, **values)

        def write(fd, raw):
            cleanup = state.get('cleanup')
            if cleanup is not None and fd == cleanup.fd and (
                    (moment == 'intent_write' and state['current'] == 'INTENT') or
                    (moment == 'verified_write' and state['current'] == 'VERIFIED')):
                raise OSError(errno.EIO, 'evidence write failure')
            if cleanup is not None and fd == cleanup.fd and moment == 'torn_intent' and state['current'] == 'INTENT':
                real_write(fd, raw[:len(raw) // 2])
                raise OSError(errno.EIO, 'partial evidence write failure')
            return real_write(fd, raw)

        def fsync(fd):
            cleanup = state.get('cleanup')
            if cleanup is not None and (
                    (moment == 'intent_fsync' and state['current'] == 'INTENT' and fd == cleanup.fd) or
                    (moment == 'intent_parent_fsync' and state['current'] == 'INTENT' and fd == cleanup.parent_fd) or
                    (moment == 'parent_fsync' and state.get('rmdir') and fd == cleanup.pins['old']) or
                    (moment == 'complete_fsync' and state['current'] == 'COMPLETE' and fd == cleanup.fd)):
                raise OSError(errno.EIO, 'fsync failure')
            return real_fsync(fd)

        def rmdir(name, *, dir_fd=None):
            assert name == 'copy' and dir_fd == owner._directories.pins['old']
            assert records(owner)[-1]['state'] == 'INTENT'
            if moment == 'rmdir':
                raise OSError(errno.EIO, 'rmdir result unconfirmed')
            if moment == 'false_success':
                return
            if moment == 'renamed':
                # Path absence alone must not prove that the held inode was
                # removed. Simulate a lying syscall boundary moving it aside.
                return os.rename(name, 'detached-cleanup', src_dir_fd=dir_fd, dst_dir_fd=owner._database._parent)
            result = real_rmdir(name, dir_fd=dir_fd)
            if moment == 'rmdir_after':
                raise OSError(errno.EIO, 'rmdir succeeded but return unconfirmed')
            state['rmdir'] = True
            return result

        def decision(proposal):
            observed.update(db=path.read_bytes(), rows=database_rows(path), journal=owner.journal_path.read_bytes(),
                files={a.target: (owner.root / a.target).read_bytes() for a in manifest.payload.actions})
            monkeypatch.setattr(api._Cleanup, '_append', appended)
            monkeypatch.setattr(os, 'write', write)
            monkeypatch.setattr(os, 'fsync', fsync)
            monkeypatch.setattr(os, 'rmdir', rmdir)
            return api.approve_cleanup(proposal, ('old/copy',), actor='operator', reason='reviewed')

        with pytest.raises(stops.ScratchFilesystemReconciliationRequired):
            run(owner, manifest, path, decision)
        removed = moment in {'rmdir_after', 'renamed', 'parent_fsync', 'verified_write', 'complete_fsync'}
        assert (owner.root / 'old/copy').exists() is not removed
        assert (owner.root / 'old/OST').is_dir()
        assert path.read_bytes() == observed['db'] and database_rows(path) == observed['rows']
        assert owner.journal_path.read_bytes() == observed['journal']
        assert {a.target: (owner.root / a.target).read_bytes() for a in manifest.payload.actions} == observed['files']
        assert owner.cleanup_outcome is None
        prefix = owner.cleanup_evidence_path.read_bytes()
        with pytest.raises(stops.ScratchCoordinationStopped):
            owner.execute(manifest, cleanup_decision=decision)
        assert owner.cleanup_evidence_path.read_bytes() == prefix
        assert_no_case_descriptors(owner)


def test_later_failure_keeps_verified_removal_and_unattempted_directories(tmp_path, monkeypatch):
    api = cleanup_api()
    with ScratchManifestExecution.create(tmp_path) as owner:
        manifest, path = prepare(owner)
        rmdir = os.rmdir
        def removed(name, *, dir_fd=None):
            if name == 'copy':
                raise OSError(errno.EIO, 'second removal rejected')
            return rmdir(name, dir_fd=dir_fd)
        def decision(proposal):
            monkeypatch.setattr(os, 'rmdir', removed)
            return api.approve_cleanup(proposal, ('old', 'old/OST', 'old/copy'), actor='operator', reason='reviewed')
        with pytest.raises(stops.ScratchFilesystemReconciliationRequired):
            run(owner, manifest, path, decision)
        assert not (owner.root / 'old/OST').exists()
        assert (owner.root / 'old/copy').is_dir() and (owner.root / 'old').is_dir()
        assert [(r['state'], r.get('path')) for r in records(owner)] == [
            ('BOUND', None), ('INTENT', 'old/OST'), ('VERIFIED', 'old/OST'), ('INTENT', 'old/copy')]


@pytest.mark.parametrize('race', ['after_intent', 'at_rmdir', 'after_parent_fsync'])
def test_unknown_content_at_cleanup_barriers_is_preserved_and_stops_sequence(tmp_path, monkeypatch, race):
    api = cleanup_api()
    with ScratchManifestExecution.create(tmp_path) as owner:
        manifest, path = prepare(owner)
        append, rmdir, fsync = api._Cleanup._append, os.rmdir, os.fsync
        state = {}
        def inject():
            asset = owner.root / ('old/copy/unknown.bin' if race != 'after_parent_fsync' else 'empty/unapproved/unknown.bin')
            asset.write_bytes(b'unknown content must survive')
            state['asset'] = asset
        def appended(cleanup, current, **values):
            result = append(cleanup, current, **values)
            if current == 'INTENT' and race == 'after_intent':
                inject()
            return result
        def removed(name, *, dir_fd=None):
            if race == 'at_rmdir':
                inject()
            result = rmdir(name, dir_fd=dir_fd)
            state['removed'] = True
            return result
        def synced(fd):
            result = fsync(fd)
            if race == 'after_parent_fsync' and state.get('removed') and fd == owner._directories.pins['old']:
                inject()
            return result
        def decision(proposal):
            monkeypatch.setattr(api._Cleanup, '_append', appended)
            monkeypatch.setattr(os, 'rmdir', removed)
            monkeypatch.setattr(os, 'fsync', synced)
            return api.approve_cleanup(proposal, ('old/copy',), actor='operator', reason='reviewed')
        with pytest.raises(stops.ScratchFilesystemReconciliationRequired):
            run(owner, manifest, path, decision)
        assert state['asset'].read_bytes() == b'unknown content must survive'
        assert (owner.root / 'old/copy').exists() == (race != 'after_parent_fsync')
        assert (owner.root / 'old/OST').is_dir()
        assert owner.cleanup_outcome is None


@pytest.mark.parametrize('moment', ['before', 'after', 'verified'])
def test_sigkill_retains_evidence_and_actual_partial_state(tmp_path, moment):
    helper = Path(__file__).with_name('v6_cleanup_restart_helper.py')
    child = subprocess.run([sys.executable, str(helper), str(tmp_path), moment],
        capture_output=True, text=True, timeout=90)
    assert child.returncode == -signal.SIGKILL, child.stderr
    case = json.loads((tmp_path / 'case.json').read_text())
    root, path = Path(case['root']), Path(case['database'])
    evidence = [json.loads(line) for line in root.with_name(root.name + '.cleanup.jsonl').read_bytes().splitlines()]
    assert evidence[-1]['state'] == ('VERIFIED' if moment == 'verified' else 'INTENT')
    assert not any(r['state'] == 'COMPLETE' for r in evidence)
    assert (root / 'old/copy').exists() == (moment == 'before')
    assert (root / 'old/OST').is_dir()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == case['db_sha256']
    assert hashlib.sha256(root.with_name(root.name + '.journal.jsonl').read_bytes()).hexdigest() == case['journal_sha256']
    assert {p: (root / p).read_bytes().hex() for p in case['files']} == case['files']


def test_declined_cleanup_preserves_every_directory_and_creates_no_cleanup_evidence(tmp_path):
    with ScratchManifestExecution.create(tmp_path) as owner:
        manifest, path = prepare(owner)
        run(owner, manifest, path, lambda proposal: None)
        assert (owner.root / 'old/copy').is_dir()
        assert not owner.cleanup_evidence_path.exists()
        assert owner.cleanup_outcome.removed == ()


@pytest.mark.parametrize('selected', [('',), ('.',), ('../old',), ('Show',), ('Subs',), ('Subs/empty',),
    ('old',), ('old/copy', 'old/copy'), ('missing',)])
def test_invalid_or_protected_directory_approval_never_removes_anything(tmp_path, selected):
    api = cleanup_api()
    with ScratchManifestExecution.create(tmp_path) as owner:
        manifest, path = prepare(owner)
        def decision(proposal):
            return api.approve_cleanup(proposal, selected, actor='operator', reason='explicit test')
        with pytest.raises(stops.ScratchCoordinationStopped):
            run(owner, manifest, path, decision)
        assert (owner.root / 'old/copy').is_dir()
        assert not owner.cleanup_evidence_path.exists()
        assert_no_case_descriptors(owner)
