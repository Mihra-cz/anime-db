"""Claude's actual CIFS stale-readdir mechanism inside the live E7H workflow."""
import errno
import hashlib
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
from app.target_execution_manifest import manifest_to_json
from app.tools.reorganize import execute_approved_reorganization
from test_v6_cifs_checkpoints import cifs_case  # noqa: F401
from test_v6_execution_projection import expected_rows
from v6_database_case import database_rows

NESTED=('Downloads','Downloads/Alpha','old','old/copy','temp/beta s2')


@pytest.mark.parametrize('scenario',['nested','lab_selection','preserve','missing_verified',
    'recreated_directory','recreated_symlink','confirmation_error','foreign_enoent','changed_directory'])
def test_live_e7h_with_proven_stale_readdir(cifs_case,monkeypatch,request,scenario):
    root,database,manifest,before,args=cifs_case
    server=Path(os.environ['ANIMEDB_CIFS_CHECKPOINT_SERVER'])/root.name
    selected=() if scenario in {'preserve','changed_directory'} else (
        ('old/copy','temp/beta s2') if scenario=='lab_selection' else NESTED)
    original_rmdir=os.rmdir
    original_names=cleanup._Cleanup._snapshot_names
    original_stat=cleanup._Cleanup._stat
    original_append=cleanup._Cleanup._append
    evidence={'stale':0,'old_stale':0,'held_enumerations':0,'statx_enoent':0,'accepted':0,'rmdir':[],'metadata_enoent':{},'recreated':False,'foreign':False,'stages':[]}
    proposals=[]
    # Keep a real enumeration handle alive, as in Claude's mechanism probe.
    parent_paths={'.'}|{p.rsplit('/',1)[0] for p in selected if '/' in p and p.rsplit('/',1)[0] not in selected}
    enumeration_pins={p:os.open(root/p,fs._DIRECTORY) for p in parent_paths}
    for pin in enumeration_pins.values():request.addfinalizer(lambda pin=pin:os.close(pin))

    def rmdir(name,*,dir_fd=None):
        if dir_fd is None:return original_rmdir(name)
        # Claude mechanism: sync/list the same held parent immediately before
        # rmdir while the candidate's handle is still pinned by original E7H.
        parent=Path(os.readlink(f'/proc/self/fd/{dir_fd}'))
        if parent==root or root in parent.parents:
            fs._sync_file_metadata(dir_fd)
            os.listdir(dir_fd)
            evidence['rmdir'].append(str((parent/name).relative_to(root)))
        return original_rmdir(name,dir_fd=dir_fd)
    def names(value,parent):
        result=original_names(value,parent)
        directory=Path(os.readlink(f'/proc/self/fd/{parent}'))
        relative_parent=str(directory.relative_to(root))
        if (scenario not in {'nested','lab_selection','preserve','changed_directory'}
                and value.removed and relative_parent in enumeration_pins):
            # Fault scenarios consume an actual held-handle CIFS readdir result,
            # never invented names/ENOENT. The nested positive must reproduce on
            # the unmodified production enumeration, with no such substitution.
            held=sorted(os.listdir(enumeration_pins[relative_parent]))
            if any((name if relative_parent=='.' else relative_parent+'/'+name) in value.removed for name in held):
                result=held;evidence['held_enumerations']+=1
        if directory==root and value.removed:
            evidence['old_stale']+=len(set(os.listdir(enumeration_pins['.']))&set(value.removed))
        for name in result:
            relative=str((directory/name).relative_to(root))
            if relative in value.removed:
                try:fs._sync_file_metadata(parent,name=name)
                except FileNotFoundError as error:
                    assert error.errno==errno.ENOENT
                    evidence['stale']+=1;evidence['statx_enoent']+=1
        return result
    def stat(value,fd,name=None):
        relative=None
        if name is not None:
            relative=str((Path(os.readlink(f'/proc/self/fd/{fd}'))/name).relative_to(root))
        own=relative in value.removed and not value._active_paths
        if own and evidence['metadata_enoent'].get(relative,0):
            if scenario=='confirmation_error':raise OSError(errno.EIO,'test: confirmation unavailable')
            if scenario in {'recreated_directory','recreated_symlink'} and (server/relative).parent.is_dir() and not evidence['recreated']:
                if scenario=='recreated_directory':(server/relative).mkdir()
                else:(server/relative).symlink_to(server/'empty/unapproved',target_is_directory=True)
                evidence['recreated']=True
        if scenario=='foreign_enoent' and value.removed and relative=='empty/unapproved' and not evidence['foreign']:
            # An actual unapproved disappearance after enumeration, not a fabricated ENOENT.
            (server/relative).rmdir();evidence['foreign']=True
        try:return original_stat(value,fd,name)
        except FileNotFoundError as error:
            if own:evidence['metadata_enoent'][relative]=evidence['metadata_enoent'].get(relative,0)+1
            raise
    def append(value,state,**kwargs):
        if scenario=='missing_verified' and state=='VERIFIED' and kwargs.get('path')=='old':return
        result=original_append(value,state,**kwargs)
        if state=='VERIFIED':
            stale=[]
            for parent,pin in enumeration_pins.items():
                for name in os.listdir(pin):
                    path=name if parent=='.' else parent+'/'+name
                    if path in value.removed:stale.append(path)
            evidence['stages'].append({'path':kwargs['path'],'stale_pinned':sorted(stale)})
        return result
    monkeypatch.setattr(os,'rmdir',rmdir)
    monkeypatch.setattr(cleanup._Cleanup,'_snapshot_names',names)
    monkeypatch.setattr(cleanup._Cleanup,'_stat',stat)
    monkeypatch.setattr(cleanup._Cleanup,'_append',append)
    accepted=getattr(cleanup._Cleanup,'_removed_entry_absent',None)
    if accepted is not None:
        def recording(value,*a,**kw):
            result=accepted(value,*a,**kw)
            if result:evidence['accepted']+=1
            return result
        monkeypatch.setattr(cleanup._Cleanup,'_removed_entry_absent',recording)
    def decide(proposal):
        proposals.append(proposal)
        assert proposal.proposal_sha256==cleanup._seal(proposal,'proposal_sha256')
        for pin in enumeration_pins.values():
            fs._sync_file_metadata(pin)
            os.listdir(pin)
        time.sleep(2.5)
        if scenario=='changed_directory':
            info=(root/'empty').stat()
            os.utime(root/'empty',ns=(info.st_atime_ns,info.st_mtime_ns+2*10**9))
            return None
        return cleanup.approve_cleanup(proposal,tuple(selected),actor='CIFS mechanism test',reason='exact subset') if selected else None
    args['cleanup_decision']=decide
    positive=scenario in {'nested','lab_selection','preserve'}
    try:
        if positive:
            result=execute_approved_reorganization(**args)
            assert result['state']=='NORMAL' and sorted(result['removed_directories'])==sorted(selected)
            assert _require_no_evidence(database).__class__.__name__=='NormalOperation'
            if selected:
                if scenario=='nested':
                    assert evidence['stale']>0,'test did not reproduce stale readdir'
                    assert evidence['statx_enoent']>0 and evidence['accepted']>0
                frames=cleanup._cleanup_frames(database.with_suffix('.cleanup.jsonl').read_bytes())
                order=sorted(selected,key=lambda p:(-p.count('/'),p))
                assert [r['path'] for r in frames if r['state']=='INTENT']==order
                assert [r['path'] for r in frames if r['state']=='VERIFIED']==order
                assert frames[-1]['state']=='COMPLETE'
                for path in selected:
                    assert not os.path.lexists(server/path)
                    assert evidence['rmdir'].count(path)==1
                for candidate in proposals[0].candidates:
                    if candidate.path not in selected:assert (server/candidate.path).is_dir()
            else:
                assert not evidence['rmdir'] and not database.with_suffix('.cleanup.jsonl').exists()
        else:
            with pytest.raises(ScratchFilesystemReconciliationRequired):
                execute_approved_reorganization(**args)
            from app.maintenance import MaintenanceStopped
            with pytest.raises(MaintenanceStopped):_require_no_evidence(database)
            if scenario!='changed_directory':
                assert evidence['stale']>0 and evidence['statx_enoent']>0
                assert '"state":"COMPLETE"' not in database.with_suffix('.cleanup.jsonl').read_text()
            for path in selected:assert evidence['rmdir'].count(path)==1
            if scenario.startswith('recreated'):assert evidence['recreated']
            if scenario=='foreign_enoent':assert evidence['foreign']
        entries=journal.load_journal(database.with_suffix('.journal.jsonl'),manifest).entries
        assert len(entries)==5*len(manifest.payload.actions) and entries[-1].state=='COMPLETE'
        assert database_rows(database)==expected_rows(before,manifest.payload.locator_patches)
    finally:
        (database.parent/'stale-mechanism.json').write_text(json.dumps(evidence,sort_keys=True,indent=2))
        print(json.dumps({'scenario':scenario,**evidence},sort_keys=True))
