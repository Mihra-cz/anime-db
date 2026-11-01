"""Reopened E7 checkpoints retain content and namespace safety without live-library pins."""
import os
import hashlib
from dataclasses import replace
import sys

import pytest

from app import target_execution_cleanup as cleanup
from app import target_execution_coordination as stops
from app import target_execution_journal as journal
from app import target_execution_identity as identity
from app.target_execution_sequence import ScratchManifestExecution
from test_v6_execution_cleanup import prepare, run
from test_v6_execution_file_actions import generate
from test_v6_execution_sequence import prepare_sequence
from app.maintenance import ScratchMaintenance
from test_v6_production import production


@pytest.mark.parametrize('namespace', ['IMPORT', '#recycle'])
def test_excluded_directory_content_change_during_cleanup_review_is_irrelevant(tmp_path, namespace):
    with ScratchManifestExecution.create(tmp_path) as owner:
        _, database = prepare(owner)
        excluded = owner.root / namespace
        excluded.mkdir(mode=0o700)
        (excluded / 'existing.mkv').write_bytes(b'excluded content')
        identity = excluded.stat().st_ino
        manifest = generate(owner, database)

        def decide(proposal):
            # This is deliberately outside the operational writer exclusion;
            # only the excluded directory's own identity belongs to E7H.
            (excluded / 'new.mkv').write_bytes(b'new excluded content')
            return cleanup.approve_cleanup(proposal, ('old/copy',), actor='operator', reason='empty leaf only')

        run(owner, manifest, database, decide)
        assert not (owner.root / 'old/copy').exists()
        assert excluded.stat().st_ino == identity
        assert (excluded / 'existing.mkv').read_bytes() == b'excluded content'
        assert (excluded / 'new.mkv').read_bytes() == b'new excluded content'
        assert cleanup.read_completed_cleanup(database, manifest, owner.root) == owner.cleanup_outcome


@pytest.mark.parametrize('namespace', ['IMPORT', '#recycle'])
def test_excluded_directory_replacement_during_review_stops_before_rmdir(tmp_path, namespace):
    with ScratchManifestExecution.create(tmp_path) as owner:
        _, database = prepare(owner)
        excluded = owner.root / namespace
        excluded.mkdir(mode=0o700)
        manifest = generate(owner, database)

        def decide(proposal):
            original = excluded.stat()
            excluded.rename(tmp_path / 'original-excluded')
            excluded.mkdir(mode=0o700)
            os.utime(excluded, ns=(original.st_atime_ns, original.st_mtime_ns))
            return cleanup.approve_cleanup(proposal, ('old/copy',), actor='operator', reason='empty leaf')

        with pytest.raises(stops.ScratchCoordinationStopped):
            run(owner, manifest, database, decide)
        assert (owner.root / 'old/copy').is_dir()
        assert excluded.is_dir() and (tmp_path / 'original-excluded').is_dir()
        assert not owner.cleanup_evidence_path.exists()


@pytest.mark.parametrize('change', ['same_size_restored_mtime', 'inode', 'missing_evidence'])
def test_completed_action_reopen_detects_tampering_before_next_move(tmp_path, monkeypatch, change):
    with ScratchManifestExecution.create(tmp_path) as owner:
        manifest, database = prepare_sequence(owner, count=2)
        move = owner._run_move
        injected = []

        def completed(index):
            result = move(index)
            if index == 0:
                action = manifest.payload.actions[index]
                target = owner.root / action.target
                info = target.stat()
                if change == 'same_size_restored_mtime':
                    raw = target.read_bytes()
                    target.write_bytes(bytes([raw[0] ^ 1]) + raw[1:])
                    os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))
                elif change == 'inode':
                    replacement = target.with_name('replacement.tmp')
                    replacement.write_bytes(target.read_bytes())
                    replacement.chmod(0o600)
                    os.utime(replacement, ns=(info.st_atime_ns, info.st_mtime_ns))
                    replacement.replace(target)
                else:
                    owner.evidence_paths[0].rename(owner.evidence_paths[0].with_suffix('.missing'))
                injected.append(True)
            return result

        monkeypatch.setattr(owner, '_run_move', completed)
        with ScratchMaintenance(database, owner.root).maintenance() as lease:
            with pytest.raises(stops.ScratchCoordinationStopped):
                owner.execute(manifest, maintenance_owner=lease)
        assert injected
        entries = journal.load_journal(owner.journal_path, manifest).entries
        assert all(entry.action_index == 0 for entry in entries)


def test_final_sha_rejects_changed_bytes_even_when_intermediate_metadata_is_equal(tmp_path, monkeypatch):
    """A coarse/stale CIFS stat must not turn checkpoint reuse into content proof."""
    with ScratchManifestExecution.create(tmp_path) as owner:
        manifest, database = prepare_sequence(owner, count=2)
        move, real_stat = owner._run_move, identity._file_stat
        masked = {}

        def stat_evidence(info):
            value = real_stat(info)
            return replace(value, ctime_ns=masked[value.st_ino]) if value.st_ino in masked else value

        def completed(index):
            result = move(index)
            if index == 0:
                target = owner.root / manifest.payload.actions[0].target
                before, raw = target.stat(), target.read_bytes()
                masked[before.st_ino] = before.st_ctime_ns
                target.write_bytes(bytes([raw[0] ^ 1]) + raw[1:])
                os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
            return result

        for module in tuple(sys.modules.values()):
            if module is not None and getattr(module, '__name__', '').startswith('app.'):
                if getattr(module, '_file_stat', None) is real_stat:
                    monkeypatch.setattr(module, '_file_stat', stat_evidence)
        monkeypatch.setattr(owner, '_run_move', completed)
        with ScratchMaintenance(database, owner.root).maintenance() as lease:
            with pytest.raises(stops.ScratchCoordinationStopped):
                owner.execute(manifest, maintenance_owner=lease,
                    verify_content=(manifest.payload.actions[0].source,))
        entries = journal.load_journal(owner.journal_path, manifest).entries
        assert len(entries) == 5 * len(manifest.payload.actions)
        assert entries[-1].state == 'COMPLETE'


@pytest.mark.parametrize('namespace', ['IMPORT', '#recycle'])
def test_excluded_content_change_reaches_normal_and_independent_ro_startup(production, monkeypatch, namespace):
    import test_v6_production as workflow
    from test_v6_p2c_rescan import approve

    owner, database, manifest, runtime = production
    excluded = owner.root / namespace
    if not excluded.exists():
        excluded.mkdir(mode=0o700)
        manifest = approve(generate(owner, database))
        runtime = replace(runtime, snapshot_manifest_id=manifest.manifest_id,
            db_backup_manifest_id=manifest.manifest_id)
    original_approval = workflow.approve_cleanup

    def changed(proposal, *args, **kwargs):
        (excluded / 'new-unmanaged-file.mkv').write_bytes(b'excluded during cleanup review')
        return original_approval(proposal, *args, **kwargs)

    monkeypatch.setattr(workflow, 'approve_cleanup', changed)
    workflow.test_existing_database_cleanup_normal_and_ro_startup(
        (owner, database, manifest, runtime), monkeypatch)
    assert (excluded / 'new-unmanaged-file.mkv').read_bytes() == b'excluded during cleanup review'


def test_directory_preparation_does_not_rehash_archive_inventory_per_mkdir(tmp_path, monkeypatch):
    from test_v6_execution_file_actions import prepare_mixed
    digest = hashlib.file_digest
    repeated = []
    with ScratchManifestExecution.create(tmp_path) as owner:
        manifest, database = prepare_mixed(owner)

        def checked(source, *args, **kwargs):
            if owner._phase == 'DIRECTORY_PREPARATION' and str(source.name).endswith('.zip'):
                repeated.append(source.name)
            return digest(source, *args, **kwargs)

        monkeypatch.setattr(hashlib, 'file_digest', checked)
        with ScratchMaintenance(database, owner.root).maintenance() as lease:
            owner.execute(manifest, maintenance_owner=lease)
        assert not repeated
        assert any(a.object_kind == 'subtitle_archive' for a in manifest.payload.actions)
