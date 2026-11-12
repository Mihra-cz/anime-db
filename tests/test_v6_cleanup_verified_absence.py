"""Only a live, journal-VERIFIED own removal can explain a stale ENOENT."""
import errno
import hashlib
import os
from dataclasses import replace
from pathlib import Path

import pytest

from app import target_execution_cleanup as cleanup
from app import target_execution_coordination as stops
from app.target_execution_guard import _require_no_evidence
from app.target_execution_manifest import manifest_to_json
from app.target_planner_types import FilesystemEntry
from app.tools.reorganize import execute_approved_reorganization
from test_v6_production import production, SHARE  # noqa: F401


@pytest.mark.parametrize('scenario', ['own_verified','foreign_missing','missing_verified',
    'recreated_directory','recreated_symlink','confirmation_error','absent_race'])
def test_stale_enoent_requires_verified_own_removal_and_current_absence(production, monkeypatch, scenario):
    owner, database, manifest, runtime = production
    root = owner.root
    owner.close()
    lock = database.parent/'runtime.lock'
    lock.write_bytes(b'');lock.chmod(0o600)
    original_names = cleanup._Cleanup._snapshot_names
    original_stat = cleanup._Cleanup._stat
    original_append = cleanup._Cleanup._append
    original_absent = cleanup.fs._absent
    observations=[]
    def names(value, parent):
        result=original_names(value,parent)
        if value.removed and Path(os.readlink(f'/proc/self/fd/{parent}'))==root/'old':
            result += ['copy']
            if scenario=='foreign_missing':result+=['foreign-missing']
        return sorted(set(result))
    def stat(value, fd, name=None):
        own = (name=='copy' and 'old/copy' in value.removed and not value._active_paths
            and Path(os.readlink(f'/proc/self/fd/{fd}'))==root/'old')
        if own and observations:
            if scenario=='recreated_directory' and not (root/'old/copy').exists():
                (root/'old/copy').mkdir(mode=0o700)
            if scenario=='recreated_symlink' and not (root/'old/copy').is_symlink():
                (root/'old/copy').symlink_to(root/'empty/unapproved',target_is_directory=True)
            if scenario=='confirmation_error':
                raise OSError(errno.EIO,'server confirmation unavailable')
        try:return original_stat(value,fd,name)
        except FileNotFoundError as error:
            if own:observations.append(error.errno)
            raise
    def append(value,state,**kwargs):
        if scenario=='missing_verified' and state=='VERIFIED':return
        return original_append(value,state,**kwargs)
    def absent(parent,name):
        if (scenario=='absent_race' and name=='copy' and len(observations)>=2
                and Path(os.readlink(f'/proc/self/fd/{parent}'))==root/'old'):
            (root/'old/copy').mkdir(mode=0o700)
        return original_absent(parent,name)
    monkeypatch.setattr(cleanup._Cleanup,'_snapshot_names',names)
    monkeypatch.setattr(cleanup._Cleanup,'_stat',stat)
    monkeypatch.setattr(cleanup._Cleanup,'_append',append)
    monkeypatch.setattr(cleanup.fs,'_absent',absent)
    def decide(proposal):
        return cleanup.approve_cleanup(proposal,('old/copy',),actor='test',reason='one exact directory')
    args=dict(database=database,library_root=root,share=SHARE,share_root='/',manifest=manifest,
        approved_sha256=hashlib.sha256(manifest_to_json(manifest).encode()).hexdigest(),
        runtime=runtime,runtime_lock=lock,cleanup_decision=decide)
    if scenario=='own_verified':
        result=execute_approved_reorganization(**args)
        assert result['state']=='NORMAL'
        assert observations and len(observations)>=2 and set(observations)=={errno.ENOENT}
        frames=cleanup._cleanup_frames(database.with_suffix('.cleanup.jsonl').read_bytes())
        assert [r['state'] for r in frames]==['BOUND','INTENT','VERIFIED','COMPLETE']
        assert _require_no_evidence(database).__class__.__name__=='NormalOperation'
    else:
        with pytest.raises(stops.ScratchFilesystemReconciliationRequired):
            execute_approved_reorganization(**args)
        from app.maintenance import MaintenanceStopped
        with pytest.raises(MaintenanceStopped):_require_no_evidence(database)
        raw=database.with_suffix('.cleanup.jsonl').read_text()
        assert '"state":"COMPLETE"' not in raw
        assert observations


@pytest.mark.parametrize('fault',['stale_own','recreated','foreign_missing','statx_error','foreign_cached'])
def test_completion_rescan_filters_only_proven_absent_own_rows(production,monkeypatch,fault):
    owner,database,manifest,runtime=production
    root=owner.root
    owner.close()
    lock=database.parent/'runtime.lock';lock.write_bytes(b'');lock.chmod(0o600)
    original=cleanup.restore_cleanup_directories
    called=[]
    def restore(snapshot,outcome,**binding):
        called.append(True)
        # Model the ordinary scandir's stale ENOENT row, after E7H COMPLETE.
        rows=snapshot.entries+(FilesystemEntry('old/copy','inaccessible',diagnostic='stat_or_read_error:2'),)
        if fault=='recreated':(root/'old/copy').mkdir(mode=0o700)
        elif fault=='foreign_missing':(root/'empty/unapproved').rmdir()
        elif fault=='foreign_cached':
            rows+=(FilesystemEntry('foreign-missing','inaccessible',diagnostic='stat_or_read_error:2'),)
        elif fault=='statx_error':
            def unsupported():raise OSError(errno.EOPNOTSUPP,'test: sync unavailable')
            monkeypatch.setattr(cleanup.fs,'_load_statx',unsupported)
        return original(replace(snapshot,entries=rows),outcome,**binding)
    monkeypatch.setattr(cleanup,'restore_cleanup_directories',restore)
    args=dict(database=database,library_root=root,share=SHARE,share_root='/',manifest=manifest,
        approved_sha256=hashlib.sha256(manifest_to_json(manifest).encode()).hexdigest(),
        runtime=runtime,runtime_lock=lock,cleanup_decision=lambda proposal:
            cleanup.approve_cleanup(proposal,('old/copy',),actor='test',reason='exact subset'))
    if fault=='stale_own':
        assert execute_approved_reorganization(**args)['state']=='NORMAL'
        assert _require_no_evidence(database).__class__.__name__=='NormalOperation'
    else:
        with pytest.raises(stops.ScratchFilesystemReconciliationRequired):
            execute_approved_reorganization(**args)
        from app.maintenance import MaintenanceStopped
        with pytest.raises(MaintenanceStopped):_require_no_evidence(database)
    assert called
    assert cleanup._cleanup_frames(database.with_suffix('.cleanup.jsonl').read_bytes())[-1]['state']=='COMPLETE'
