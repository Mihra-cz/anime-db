"""Opt-in measurement of SMB close timing; never changes E7H or retries rmdir.

Uses the existing disposable cifs_case. Recording PASS means a complete
observation, not necessarily NORMAL: actual execution outcome is in the JSON.
"""
import json
import os
from pathlib import Path
import time

import pytest

from app import target_execution_cleanup as cleanup
from app import target_execution_filesystem as fs
from app import target_execution_journal as journal
from app.target_execution_coordination import ScratchFilesystemReconciliationRequired
from app.target_execution_guard import _require_no_evidence
from app.tools.reorganize import execute_approved_reorganization
from test_v6_cifs_checkpoints import cifs_case  # noqa: F401
from test_v6_execution_projection import expected_rows
from v6_database_case import database_rows

NESTED = ('Downloads', 'Downloads/Alpha', 'old', 'old/copy', 'temp/beta s2')
STAT_FIELDS = ('st_dev', 'st_ino', 'st_mode', 'st_nlink', 'st_uid', 'st_gid',
               'st_size', 'st_mtime_ns', 'st_ctime_ns')


def numbers(info):
    return {field: getattr(info, field) for field in STAT_FIELDS}


@pytest.fixture
def observed_case(request):
    if os.environ.get('PROBE_MOUNT_ROOT') != '1':
        return request.getfixturevalue('cifs_case')
    # Same existing fixture builders/generator/runtime contract; this variant
    # uses a separate share whose entire root was seeded before mounting it.
    import hashlib
    from app.target_execution_manifest import RuntimeEvidence, manifest_to_json
    from app.target_execution_mounts import _read_mounts
    from app.target_execution_sequence import ScratchManifestExecution
    from test_v6_execution_file_actions import generate
    from test_v6_p2c_rescan import approve
    root = Path(os.environ['ANIMEDB_CIFS_CHECKPOINT_MOUNT'])
    server = Path(os.environ['ANIMEDB_CIFS_CHECKPOINT_SERVER'])
    assert server.name.startswith('root-closetimeo-')
    assert (server.parent / '.checkpoint-probe-only').read_text() == 'DISPOSABLE ANIMEDB CIFS CHECKPOINT PROBE\n'
    covering = next(m for m in _read_mounts() if m.point == root)
    assert covering.fstype == 'cifs' and str(covering.fs_root) == '/'
    database = Path(os.environ['PROBE_ROOT_DATABASE'])
    assert database.is_file() and database.parent.name.startswith('rootseed-')
    with ScratchManifestExecution.open_existing(root, database_path=database,
            share=covering.source, share_root='/') as owner:
        manifest = approve(generate(owner, database))
        backup = database.parent / 'backup.sqlite'
        backup.write_bytes(database.read_bytes())
        p = manifest.payload.db_fingerprint
        runtime = RuntimeEvidence(scanner_paused_through_verification=True,
            inventory_writer_paused_through_verification=True, snapshot_capable=True,
            snapshot_id='isolated-root-close-probe', snapshot_at=manifest.created_at,
            snapshot_manifest_id=manifest.manifest_id, db_backup_path=str(backup),
            db_backup_sha256=p.sha256, db_backup_size=p.size,
            db_backup_user_version=p.user_version, db_backup_manifest_id=manifest.manifest_id,
            actual_windows_root=manifest.payload.windows_root, mount_identity=owner.mount_identity,
            mount_read_only_now=False, free_bytes=10**12, mount_read_write_checked=True,
            safe_write_capability_proven=True, rename_noreplace_capability_proven=True)
    lock = database.parent / 'runtime.lock'
    lock.write_bytes(b''); lock.chmod(0o600)
    args = dict(database=database, library_root=root, share=covering.source, share_root='/',
        manifest=manifest, approved_sha256=hashlib.sha256(manifest_to_json(manifest).encode()).hexdigest(),
        runtime=runtime, runtime_lock=lock)
    return root, database, manifest, database_rows(database), args


def test_cifs_e7h_closetimeo_observation(observed_case, monkeypatch):
    root, database, manifest, before, args = observed_case
    server = Path(os.environ['ANIMEDB_CIFS_CHECKPOINT_SERVER'])
    if os.environ.get('PROBE_MOUNT_ROOT') != '1':
        server /= root.name
    close = os.environ['PROBE_CLOSETIMEO']
    assert close in ('0', '1')
    mounts = [line for line in Path('/proc/mounts').read_text().splitlines()
              if line.split()[1] == os.environ['ANIMEDB_CIFS_CHECKPOINT_MOUNT']]
    assert len(mounts) == 1 and mounts[0].split()[2] == 'cifs'
    options = mounts[0].split()[3].split(',')
    assert f'closetimeo={close}' in options and 'actimeo=1' in options
    records, active, removals = [], {}, []
    report = {'closetimeo': close, 'trial': os.environ.get('PROBE_TRIAL', '0'),
              'mount_root': os.environ.get('PROBE_MOUNT_ROOT') == '1',
              'early_absence': os.environ.get('PROBE_EARLY_ABSENCE') == '1',
              'mounts': mounts, 'kernel': os.uname().release, 'records': records}
    original_remove, original_stat = cleanup._Cleanup._remove, cleanup._Cleanup._stat
    original_run, original_append, original_rmdir = cleanup._Cleanup.run, cleanup._Cleanup._append, os.rmdir
    started = time.monotonic_ns()

    def record(value, stage, **extra):
        expected = value._expected_entries['.']
        records.append({'at_ns': time.monotonic_ns() - started, 'stage': stage,
                        'path': active.get('path'), 'expected_root': {
                            'st_dev': expected.device, 'st_ino': expected.inode,
                            'st_mode': expected.mode, 'st_nlink': expected.nlink,
                            'st_uid': expected.uid, 'st_gid': expected.gid,
                            'st_size': expected.size, 'st_mtime_ns': expected.mtime_ns,
                            'st_ctime_ns': expected.ctime_ns},
                        'server_root': numbers(server.stat()), **extra})

    def stat(value, fd, name=None):
        info = original_stat(value, fd, name)
        if active and name is None:
            first = active.get('closed') and not active.get('captured') and fd == active['parent']
            if first:
                active['captured'] = True
                # This is the original production call immediately after close.
                # Return its exact result; no extra CIFS queries precede it.
                record(value, 'parent_checkpoint', returned_parent=numbers(info))
            elif active.get('captured') and fd == value.filesystem._root_fd:
                record(value, 'subsequent_root_check', returned_root=numbers(info))
        return info

    def remove(value, path, parts, parent_path, parent, fd, *, release_candidate):
        active.update(owner=value, path=path, parent=parent, closed=False, captured=False)
        record(value, 'before_remove')
        def release():
            release_candidate()
            active['closed'] = True
            # Local backing-filesystem stat only: do not refresh the CIFS root
            # before the original E7H parent checkpoint has been captured.
            record(value, 'candidate_closed')
            if report['early_absence']:
                # Experimental ordering of the two existing read-only fences;
                # no replacement metadata, time tolerance or physical retry.
                value.filesystem._parent_binding(parts, parent)
                fs._absent(parent, parts[-1])
                record(value, 'early_absence_checked')
        try:
            return original_remove(value, path, parts, parent_path, parent, fd, release_candidate=release)
        finally:
            active.clear()

    def rmdir(name, *, dir_fd=None):
        if active and dir_fd == active['parent']:
            record(active['owner'], 'before_rmdir', cached_root=numbers(os.fstat(active['owner'].filesystem._root_fd)))
            # Same plain-listdir probe as Claude's independent reproduction.
            os.listdir(dir_fd)
            result = original_rmdir(name, dir_fd=dir_fd)
            removals.append(active['path'])
            record(active['owner'], 'rmdir_returned')
            return result
        return original_rmdir(name, dir_fd=dir_fd) if dir_fd is not None else original_rmdir(name)

    def append(value, state, **kwargs):
        result = original_append(value, state, **kwargs)
        if active and state in {'INTENT', 'VERIFIED'}:
            record(value, 'journal_' + state)
        return result

    def run(value, decision):
        try:
            return original_run(value, decision)
        finally:
            # The execution decision has already been made. Only now compare
            # long-held and independent handles, keeping all original pins live.
            post = time.monotonic()
            for delay in (0, .2, 1.2):
                time.sleep(max(0, post + delay - time.monotonic()))
                server_before = numbers(server.stat())
                held = numbers(fs._sync_file_metadata(value.filesystem._root_fd))
                with fs._held_evidence_fd(os.open(root, fs._DIRECTORY)) as fresh:
                    reopened = numbers(fs._sync_file_metadata(fresh))
                with fs._held_evidence_fd(os.open(root.parent, fs._DIRECTORY)) as parent:
                    named = numbers(fs._sync_file_metadata(parent, name=root.name))
                record(value, 'after_cleanup_observation', delay=delay,
                       server_before=server_before, held_root=held, fresh_root=reopened,
                       named_root=named)

    monkeypatch.setattr(cleanup._Cleanup, '_remove', remove)
    monkeypatch.setattr(cleanup._Cleanup, '_stat', stat)
    monkeypatch.setattr(cleanup._Cleanup, '_append', append)
    monkeypatch.setattr(cleanup._Cleanup, 'run', run)
    monkeypatch.setattr(os, 'rmdir', rmdir)
    args['cleanup_decision'] = lambda proposal: (
        time.sleep(2.5) or cleanup.approve_cleanup(proposal, NESTED,
            actor='isolated closetimeo probe', reason='exact nested subset'))
    try:
        try:
            result = execute_approved_reorganization(**args)
        except ScratchFilesystemReconciliationRequired as exc:
            report.update(state='STOP', error=str(exc), phase=exc.phase)
            from app.maintenance import MaintenanceStopped
            with pytest.raises(MaintenanceStopped):
                _require_no_evidence(database)
        else:
            report.update(state=result['state'])
            assert result['state'] == 'NORMAL'
            assert sorted(result['removed_directories']) == sorted(NESTED)
            assert _require_no_evidence(database).__class__.__name__ == 'NormalOperation'
        path = database.with_suffix('.cleanup.jsonl')
        frames = cleanup._cleanup_frames(path.read_bytes(), terminal=json.loads(path.read_bytes().splitlines()[-1])['state'])
        report['journal'] = [{'state': row['state'], 'path': row.get('path')} for row in frames]
        report['rmdir'] = removals
        report['server_absence'] = {p: not os.path.lexists(server / p) for p in NESTED}
        if report['state'] == 'NORMAL':
            assert all(report['server_absence'].values())
            assert [row['path'] for row in frames if row['state'] == 'VERIFIED'] == sorted(NESTED, key=lambda p: (-p.count('/'), p))
            assert frames[-1]['state'] == 'COMPLETE'
        assert all(removals.count(path) <= 1 for path in NESTED)
        entries = journal.load_journal(database.with_suffix('.journal.jsonl'), manifest).entries
        assert len(entries) == 70 and entries[-1].state == 'COMPLETE'
        assert database_rows(database) == expected_rows(before, manifest.payload.locator_patches)
        if os.environ.get('PROBE_EXPECT_NORMAL') == '1':
            assert not report['early_absence'], 'final regression must use the production ordering'
            assert report['state'] == 'NORMAL', report.get('error')
            for row in records:
                if row['stage'] == 'parent_checkpoint' and row['path'] in ('Downloads', 'old'):
                    assert all(row['returned_parent'][field] == row['server_root'][field] // 100 * 100
                        for field in ('st_mtime_ns', 'st_ctime_ns'))
    finally:
        (database.parent / 'closetimeo-observation.json').write_text(json.dumps(report, sort_keys=True, indent=2))
        print(json.dumps({k: report.get(k) for k in ('closetimeo', 'trial', 'state', 'error', 'server_absence')}, sort_keys=True))
