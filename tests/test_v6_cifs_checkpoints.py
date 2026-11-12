"""Opt-in real CIFS checkpoints on a disposable share, never the preserved LAB.

ANIMEDB_CIFS_CHECKPOINT_MOUNT must contain the marker below. Every case creates
its own new library and local SQLite; no existing execution is opened or resumed.
"""
from pathlib import Path
import hashlib
import os
import shutil
import sqlite3
import time
import uuid

import pytest

from app import target_execution_journal as journal
from app.target_execution_coordination import ScratchCoordinationStopped
from app.target_execution_guard import _require_no_evidence
from app.target_execution_manifest import RuntimeEvidence, manifest_to_json
from app.target_execution_sequence import ScratchManifestExecution
from app.tools.reorganize import execute_approved_reorganization
from deploy.lab.make_lab_data import build
from test_v6_execution_file_actions import generate
from test_v6_execution_projection import expected_rows
from test_v6_p2c_rescan import approve
from v6_database_case import database_rows

MARKER = 'DISPOSABLE ANIMEDB CIFS CHECKPOINT PROBE\n'


@pytest.fixture
def cifs_case(tmp_path, monkeypatch):
    configured = os.environ.get('ANIMEDB_CIFS_CHECKPOINT_MOUNT')
    if not configured:
        pytest.skip('requires a separate disposable CIFS share')
    mount = Path(configured).resolve(strict=True)
    assert (mount / '.checkpoint-probe-only').read_text() == MARKER
    from app.target_execution_mounts import _read_mounts
    covering = max((m for m in _read_mounts() if mount.is_relative_to(m.point)),
        key=lambda m: len(m.point.parts))
    assert covering.fstype == 'cifs'
    assert covering.point == mount and str(covering.fs_root) == '/'
    local_root, database = build(tmp_path / 'synthetic')
    relative = 'checkpoint-' + uuid.uuid4().hex
    root = mount / relative
    shutil.copytree(local_root, root)  # unique new path; never an existing library
    time.sleep(3)  # settle initial writes; this is an experiment, not an E7 retry
    with ScratchManifestExecution.open_existing(root, database_path=database,
            share=covering.source, share_root='/' + relative) as owner:
        manifest = approve(generate(owner, database))
        backup = database.parent / 'backup.sqlite'
        backup.write_bytes(database.read_bytes())
        p = manifest.payload.db_fingerprint
        runtime = RuntimeEvidence(scanner_paused_through_verification=True,
            inventory_writer_paused_through_verification=True, snapshot_capable=True,
            snapshot_id='disposable-checkpoint-test', snapshot_at=manifest.created_at,
            snapshot_manifest_id=manifest.manifest_id, db_backup_path=str(backup),
            db_backup_sha256=p.sha256, db_backup_size=p.size,
            db_backup_user_version=p.user_version, db_backup_manifest_id=manifest.manifest_id,
            actual_windows_root=manifest.payload.windows_root, mount_identity=owner.mount_identity,
            mount_read_only_now=False, free_bytes=10**12, mount_read_write_checked=True,
            safe_write_capability_proven=True, rename_noreplace_capability_proven=True)
    lock = database.parent / 'runtime.lock'
    lock.write_bytes(b'')
    lock.chmod(0o600)
    before = database_rows(database)
    canonical = manifest_to_json(manifest)
    proposals = []
    def preserve(proposal):
        proposals.append(proposal.proposal_sha256)
        return None
    preserve.proposals = proposals
    args = dict(database=database, library_root=root, share=covering.source,
        share_root='/' + relative, manifest=manifest,
        approved_sha256=hashlib.sha256(canonical.encode()).hexdigest(), runtime=runtime,
        runtime_lock=lock, cleanup_decision=preserve)
    yield root, database, manifest, before, args
    # Deliberately retain all new case data/evidence for independent review.


def at_final_db_pre_update(monkeypatch, manifest, action):
    original = ScratchManifestExecution._verify_checkpoint
    fired = []

    def check(owner, index, *, full):
        if (not fired and owner._phase == 'DB_PRE_UPDATE'
                and owner._active_index == len(manifest.payload.actions) - 1 and index == 0):
            fired.append(True)
            action(owner)
            time.sleep(2.2)  # release SMB deferred-close lease before old checkpoint reopen
        return original(owner, index, full=full)

    monkeypatch.setattr(ScratchManifestExecution, '_verify_checkpoint', check)
    return fired


@pytest.mark.parametrize('full', [False, True])
def test_cifs_delayed_final_checkpoint_reaches_normal(cifs_case, monkeypatch, full):
    root, database, manifest, before, args = cifs_case
    assert len(manifest.payload.actions) == 14
    if full:
        args['verify_content'] = (manifest.payload.actions[0].source,)
    fired = at_final_db_pre_update(monkeypatch, manifest, lambda owner: None)
    result = execute_approved_reorganization(**args)
    assert fired and result['state'] == 'NORMAL'
    entries = journal.load_journal(database.with_suffix('.journal.jsonl'), manifest).entries
    assert len(entries) == 5 * 14 and entries[-1].state == 'COMPLETE'
    assert database_rows(database) == expected_rows(before, manifest.payload.locator_patches)
    assert len(args['cleanup_decision'].proposals) == 1
    assert result['removed_directories'] == []
    assert not database.with_suffix('.cleanup.jsonl').exists()  # keep-all has no rmdir journal
    assert _require_no_evidence(database).__class__.__name__ == 'NormalOperation'


@pytest.mark.parametrize('change', ['same_size_restored_mtime', 'replacement'])
def test_cifs_changed_completed_target_stops_before_last_db_update(cifs_case, monkeypatch, change):
    root, database, manifest, before, args = cifs_case
    def tamper(owner):
        action = manifest.payload.actions[0]
        target = root / action.target
        info, raw = target.stat(), target.read_bytes()
        if change == 'same_size_restored_mtime':
            target.write_bytes(bytes([raw[0] ^ 1]) + raw[1:])
        else:
            replacement = target.with_name('probe-replacement.tmp')
            replacement.write_bytes(raw)
            replacement.replace(target)
        os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))

    fired = at_final_db_pre_update(monkeypatch, manifest, tamper)
    with pytest.raises(ScratchCoordinationStopped):
        execute_approved_reorganization(**args)
    assert fired
    entries = journal.load_journal(database.with_suffix('.journal.jsonl'), manifest).entries
    complete = [e.action_index for e in entries if e.state == 'COMPLETE']
    assert complete == list(range(13))
    assert entries[-1].action_index == 13 and entries[-1].state == 'FS_VERIFIED'
    last = manifest.payload.actions[-1]
    from test_v6_execution_projection import TABLES
    with sqlite3.connect(f'file:{database}?mode=ro', uri=True) as db:
        for patch in manifest.payload.locator_patches:
            if (patch.object_kind, patch.object_id) == (last.object_kind, last.object_id):
                table = TABLES[patch.object_kind].name
                value = db.execute(f'SELECT "{patch.field}" FROM "{table}" WHERE id=?',
                    (patch.object_id,)).fetchone()[0]
                assert value == patch.before
    assert not database.with_suffix('.cleanup.jsonl').exists()
    guard = database.with_name(database.name + '.execution-guard.json')
    assert guard.exists()
    from app.maintenance import MaintenanceStopped
    with pytest.raises(MaintenanceStopped):
        _require_no_evidence(database)


def test_cifs_missing_sync_support_stops_with_fs_evidence_before_db(cifs_case, monkeypatch):
    import errno
    from app import target_execution_filesystem as fs
    root, database, manifest, before, args = cifs_case
    def unsupported():
        raise OSError(errno.EOPNOTSUPP, 'test: server metadata sync unavailable')
    monkeypatch.setattr(fs, '_load_statx', unsupported)
    with pytest.raises(ScratchCoordinationStopped):
        execute_approved_reorganization(**args)
    entries = journal.load_journal(database.with_suffix('.journal.jsonl'), manifest).entries
    assert entries[-1].state == 'FS_APPLIED'
    assert not any(e.state == 'DB_RECONCILED' for e in entries)
    assert database_rows(database) == before
    assert database.with_name(database.name + '.execution-guard.json').exists()
    assert not args['cleanup_decision'].proposals
