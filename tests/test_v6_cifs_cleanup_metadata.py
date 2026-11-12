"""Opt-in E7H review after CIFS cache/lease expiration on new synthetic data."""
import errno
import hashlib
import json
from pathlib import Path
import uuid
import os
import time

import pytest

from app import target_execution_cleanup as cleanup
from app import target_execution_journal as journal
from app.target_execution_coordination import ScratchFilesystemReconciliationRequired
from app.target_execution_manifest import manifest_to_json
from app.tools.reorganize import execute_approved_reorganization
from test_v6_cifs_checkpoints import cifs_case
from test_v6_execution_projection import expected_rows
from v6_database_case import database_rows


@pytest.mark.parametrize('decision', ['preserve', 'delete', 'lab_selection', 'nested', 'missing_directory', 'missing_file', 'changed', 'statx_failure'])
def test_cifs_cleanup_review_after_cache_window(cifs_case, decision, monkeypatch, request):
    root, database, manifest, before, args = cifs_case
    canonical = manifest_to_json(manifest)
    selected, proposals = [], []
    pins = [os.open(root/path, os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        for path in ('Downloads/Alpha', 'old', 'temp/beta s2')]
    for fd in pins:
        os.fstat(fd)  # pre-move directory cache with a live read-only handle
    def close_pins():
        while pins:
            os.close(pins.pop())
    request.addfinalizer(close_pins)

    def decide(proposal):
        assert proposal.manifest_id == manifest.manifest_id
        assert proposal.manifest_sha256 == hashlib.sha256(canonical.encode()).hexdigest()
        assert proposal.proposal_sha256 == cleanup._seal(proposal, 'proposal_sha256')
        proposals.append(proposal)
        candidates = {row.path for row in proposal.candidates}
        assert 'old/copy' in candidates and len(candidates) > 1
        # Release pre-move client handles only after the proposal was captured.
        close_pins()
        time.sleep(2.5)
        if decision == 'statx_failure':
            from app import target_execution_filesystem as fs
            def unavailable():
                raise OSError(errno.EOPNOTSUPP, 'isolated E7H sync failure')
            monkeypatch.setattr(fs, '_load_statx', unavailable)
            return None
        if decision == 'changed':
            info = root.stat()
            os.utime(root, ns=(info.st_atime_ns, info.st_mtime_ns + 2 * 10**9))
            return None
        if decision in {'delete','lab_selection','nested','missing_directory','missing_file'}:
            selected.extend(('Downloads','Downloads/Alpha','old','old/copy','temp/beta s2') if decision=='nested'
                else ('old/copy',) if decision=='delete' else ('old/copy','temp/beta s2'))
            return cleanup.approve_cleanup(proposal, tuple(selected),
                actor='isolated test operator', reason='exact approved directory subset')
        return None

    missing = []
    original_stat = cleanup._Cleanup._stat
    foreign = root/'empty/unapproved' if decision=='missing_directory' else root/next(
        action.target for action in manifest.payload.actions if action.object_kind=='video')
    def changed_after_enumeration(value, fd, name=None):
        if (decision in {'missing_directory','missing_file'} and not missing and name is not None
                and selected and len(value.removed)==len(selected)
                and Path(os.readlink(f'/proc/self/fd/{fd}'))/name == foreign):
            assert str(foreign.relative_to(root)) not in selected
            if decision=='missing_directory':os.rmdir(name,dir_fd=fd)
            else:os.unlink(name,dir_fd=fd)
            missing.append(True)
        return original_stat(value,fd,name)
    monkeypatch.setattr(cleanup._Cleanup,'_stat',changed_after_enumeration)
    args['cleanup_decision'] = decide
    if decision in {'changed', 'statx_failure','missing_directory','missing_file'}:
        with pytest.raises(ScratchFilesystemReconciliationRequired) as error:
            execute_approved_reorganization(**args)
        assert error.value.phase == 'CLEANUP'
        if decision == 'changed':
            detail = json.loads(str(error.value).split('; CLEANUP_MISMATCH_JSON=', 1)[1])
            assert detail['different_records'] >= 1
            assert any('mtime_ns' in row['differences'] for row in detail['records'])
        if decision in {'missing_directory','missing_file'}:
            assert missing and not foreign.exists()
            assert isinstance(error.value.__cause__,FileNotFoundError)
            assert error.value.__cause__.errno == errno.ENOENT
            frames = cleanup._cleanup_frames(database.with_suffix('.cleanup.jsonl').read_bytes(),terminal='VERIFIED')
            assert [frame['path'] for frame in frames if frame['state']=='VERIFIED'] == list(
                sorted(selected,key=lambda p:(-p.count('/'),p)))
            assert not any(frame['state']=='COMPLETE' for frame in frames)
        else:
            assert not database.with_suffix('.cleanup.jsonl').exists()
            assert (root / 'old/copy').is_dir()
        assert database.with_name(database.name + '.execution-guard.json').exists()
    else:
        result = execute_approved_reorganization(**args)
        assert result['state'] == 'NORMAL'
        assert sorted(result['removed_directories']) == sorted(selected)
        for candidate in proposals[0].candidates:
            assert (root / candidate.path).is_dir() == (candidate.path not in selected)
        if selected:
            outcome = cleanup.read_completed_cleanup(database, manifest, root)
            assert outcome.proposal_sha256 == proposals[0].proposal_sha256
            assert outcome.removed == tuple(sorted(selected,key=lambda p:(-p.count('/'),p)))
            assert len(outcome.preserved) > 0
            frames = cleanup._cleanup_frames(database.with_suffix('.cleanup.jsonl').read_bytes())
            order = list(sorted(selected,key=lambda p:(-p.count('/'),p)))
            assert [frame['path'] for frame in frames if frame['state']=='INTENT'] == order
            assert [frame['path'] for frame in frames if frame['state']=='VERIFIED'] == order
            assert frames[-1]['state']=='COMPLETE'
            from app.target_execution_guard import _require_no_evidence
            assert _require_no_evidence(database).__class__.__name__ == 'NormalOperation'
            (database.parent/'enumeration-result.json').write_text(json.dumps({
                'manifest_sha256':hashlib.sha256(canonical.encode()).hexdigest(),
                'proposal_sha256':outcome.proposal_sha256,'selected':order,
                'intent_count':len(order),'verified_count':len(order),'terminal':'COMPLETE',
                'outcome':result['state']},sort_keys=True))
    entries = journal.load_journal(database.with_suffix('.journal.jsonl'), manifest).entries
    assert len(entries) == 5 * len(manifest.payload.actions)
    assert entries[-1].state == 'COMPLETE'
    assert database_rows(database) == expected_rows(before, manifest.payload.locator_patches)
    assert manifest_to_json(manifest) == canonical


def test_cifs_statx_reads_directory_server_values_with_warmed_cache(tmp_path):
    from app import target_execution_filesystem as fs
    mount_env = os.environ.get('ANIMEDB_CIFS_CHECKPOINT_MOUNT')
    server_env = os.environ.get('ANIMEDB_CIFS_CHECKPOINT_SERVER')
    if not mount_env or not server_env:
        pytest.skip('requires isolated CIFS mount and its disposable local backing directory')
    mount, server = Path(mount_env), Path(server_env)
    marker = 'DISPOSABLE ANIMEDB CIFS CHECKPOINT PROBE\n'
    assert (mount/'.checkpoint-probe-only').read_text() == marker
    assert (server/'.checkpoint-probe-only').read_text() == marker
    name = 'directory-statx-' + uuid.uuid4().hex
    directory = mount/name
    directory.mkdir()
    with fs._held_evidence_fd(os.open(mount, fs._DIRECTORY)) as parent:
        with fs._held_evidence_fd(os.open(directory, fs._DIRECTORY)) as held:
            before = os.fstat(held)
            # The server change occurs BEFORE any approval snapshot. This is a
            # direct metadata-cache experiment, separate from the E7 test cases.
            original_server = (server/name).stat()
            os.utime(server/name, ns=(original_server.st_atime_ns,
                original_server.st_mtime_ns + 2*10**9))
            cached = os.fstat(held)
            remote = (server/name).stat()
            fresh = fs._sync_file_metadata(held)
            named = fs._sync_file_metadata(parent, name=name)
            assert fresh.st_mtime_ns == named.st_mtime_ns == remote.st_mtime_ns//100*100
            assert fresh.st_ctime_ns == named.st_ctime_ns == remote.st_ctime_ns//100*100
            assert fresh.st_ino == named.st_ino == remote.st_ino
            time.sleep(2.5)
            after = fs._sync_file_metadata(held)
            assert (after.st_mtime_ns, after.st_ctime_ns) == (fresh.st_mtime_ns, fresh.st_ctime_ns)
            (tmp_path/'directory-cache.json').write_text(json.dumps({
                'actimeo':os.environ.get('PROBE_ACTIMEO','1'),
                'before_mtime_ns':before.st_mtime_ns,'cached_mtime_ns':cached.st_mtime_ns,
                'server_mtime_ns':remote.st_mtime_ns,'fresh_mtime_ns':fresh.st_mtime_ns,
                'cached_ctime_ns':cached.st_ctime_ns,'fresh_ctime_ns':fresh.st_ctime_ns},sort_keys=True))


def test_cifs_rmdir_parent_metadata_after_candidate_close(tmp_path):
    from app import target_execution_filesystem as fs
    mount_env = os.environ.get('ANIMEDB_CIFS_CHECKPOINT_MOUNT')
    server_env = os.environ.get('ANIMEDB_CIFS_CHECKPOINT_SERVER')
    if not mount_env or not server_env:
        pytest.skip('requires isolated disposable Samba backing directory')
    mount, server = Path(mount_env), Path(server_env)
    assert (mount/'.checkpoint-probe-only').read_text() == 'DISPOSABLE ANIMEDB CIFS CHECKPOINT PROBE\n'
    name = 'rmdir-close-' + uuid.uuid4().hex
    parent, child = mount/name, mount/name/'child'
    child.mkdir(parents=True)
    with fs._held_evidence_fd(os.open(parent, fs._DIRECTORY)) as parent_fd:
        child_fd = os.open(child, fs._DIRECTORY)
        try:
            before = fs._sync_file_metadata(parent_fd)
            os.rmdir('child', dir_fd=parent_fd)
            during = fs._sync_file_metadata(parent_fd)
            server_during = (server/name).stat()
            assert os.fstat(child_fd).st_nlink == 0
        finally:
            os.close(child_fd)
        after = fs._sync_file_metadata(parent_fd)
        server_after = (server/name).stat()
        (tmp_path/'rmdir-close.json').write_text(json.dumps({
            'before_mtime_ns':before.st_mtime_ns,'during_mtime_ns':during.st_mtime_ns,
            'server_during_mtime_ns':server_during.st_mtime_ns,
            'after_mtime_ns':after.st_mtime_ns,'server_after_mtime_ns':server_after.st_mtime_ns},sort_keys=True))
        assert after.st_mtime_ns == server_after.st_mtime_ns//100*100
        assert after.st_ctime_ns == server_after.st_ctime_ns//100*100
