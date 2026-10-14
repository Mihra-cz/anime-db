"""Subprocess helper for E4A tests; no application startup or production input."""
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import signal
import sys

from app import target_execution_coordination as coordination
from app import target_execution_filesystem as fs
from app import target_execution_journal as journal
from app.target_execution_manifest import ExecutionManifest, _decode_type, _strict_json_load, manifest_to_json
from test_v6_execution_filesystem import prepare, seal


def create(parent, boundary, action_kind):
    # Deliberately killed while the real writer and inode pins are still live.
    with coordination.ScratchActionSession.create(parent) as session:
        manifest, source, target = prepare(session, action=action_kind)
        manifest_path = parent / 'manifest.json'
        manifest_path.write_text(manifest_to_json(manifest), encoding='utf-8')
        paths = dict(root=str(session.root), journal=str(session.journal_path),
            evidence=str(session.evidence_path), manifest=str(manifest_path))
        append, rename = journal.DurableJournal.append, fs._rename_noreplace

        def reached(expected_states, moved):
            assert source.exists() is not moved
            assert target.exists() is moved
            # The writer owns an exclusive lock, so check its held history here.
            assert [e.state for e in writer_seen[0].inspect_live(manifest)] == expected_states
            print(json.dumps(dict(paths=paths, pid=os.getpid(), boundary=boundary,
                boundary_assertions_completed=True)), flush=True)
            while True:
                signal.pause()

        writer_seen = []

        def appended(writer, entry):
            writer_seen[:] = [writer]
            if boundary == 'after_rename' and entry.state == 'FS_APPLIED':
                reached(['PENDING'], True)
            append(writer, entry)
            if boundary == 'after_applied' and entry.state == 'FS_APPLIED':
                reached(['PENDING', 'FS_APPLIED'], True)
            if boundary == 'after_verified' and entry.state == 'FS_VERIFIED':
                reached(['PENDING', 'FS_APPLIED', 'FS_VERIFIED'], True)

        def renamed(*args):
            if boundary == 'before_rename':
                reached(['PENDING'], False)
            return rename(*args)

        journal.DurableJournal.append = appended
        fs._rename_noreplace = renamed
        session.execute(manifest, manifest.payload.actions[0], action_index=0)
        raise AssertionError('expected interruption boundary was never reached')


def inspect(paths, variation):
    from app.target_execution_reconciliation import inspect_scratch_action

    # Decode strict types without filesystem I/O. E4A must get the opportunity to
    # diagnose a root replaced by a symlink before the regular validator lstats it.
    manifest = _decode_type(ExecutionManifest,
        _strict_json_load(Path(paths['manifest']).read_text(encoding='utf-8')))
    action, index = manifest.payload.actions[0], 0
    if variation == 'other_manifest':
        manifest = replace(manifest, created_at='2026-10-14T12:00:00Z')
    elif variation == 'other_action':
        action = replace(action, criticality_reason='foreign action')
    elif variation == 'other_index':
        index = 1
    elif variation == 'bool_index':
        index = False
    elif variation == 'foreign_root':
        # No serialization/validation of this forbidden root before E4A's gate.
        manifest = seal(manifest, library_root='/mnt/nas-anime')
    elif variation == 'invalid_manifest':
        manifest = replace(manifest, manifest_id='0' * 64)

    def read_only(event, args):
        if event == 'open':
            _path, mode, flags = args
            assert not flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND), (event, args)
            assert mode is None or not any(c in mode for c in 'wax+'), (event, args)
        assert event not in {'os.mkdir', 'os.rmdir', 'os.rename', 'os.remove', 'os.link',
            'os.symlink', 'os.truncate', 'os.chmod', 'os.chown', 'os.utime', 'sqlite3.connect'}, (event, args)
        if event in {'open', 'os.listdir', 'os.scandir'} and isinstance(args[0], (str, bytes)):
            assert not os.fsdecode(args[0]).startswith('/mnt/'), (event, args)

    sys.addaudithook(read_only)
    for name in ('stat', 'lstat', 'statvfs', 'readlink'):
        original = getattr(os, name)
        def local_only(path, *args, _original=original, **kwargs):
            if isinstance(path, (str, bytes, os.PathLike)):
                assert not os.fsdecode(path).startswith('/mnt/'), ('production probe', path)
            return _original(path, *args, **kwargs)
        setattr(os, name, local_only)
    # These factories would mint execution authority even if no syscall followed.
    def no_execution(*_args, **_kwargs):
        raise AssertionError('restart inspection minted an execution context')
    fs.ScratchRenameSession.create = no_execution
    coordination.ScratchActionSession.create = no_execution
    journal.DurableJournal.create = no_execution
    report = inspect_scratch_action(manifest, action, action_index=index,
        scratch_root=Path(paths['root']), journal_path=Path(paths['journal']),
        evidence_path=Path(paths['evidence']))
    if variation == 'early_exit':
        os._exit(0)  # Test that exit code zero cannot bypass completion assertions.
    assert report.outcome == 'STOP'
    assert report.manual_review_required is True
    assert report.identity_continuity == 'UNKNOWN'
    assert report.diagnostics
    assert report.manifest_id == manifest.manifest_id
    assert report.action_id == action.action_id
    assert report.action_index == (index if type(index) is int else None)
    assert report.scratch_root == paths['root']
    # Parent requires this exact terminal marker AND successful process exit.
    print(json.dumps(dict(report=asdict(report), inspector_pid=os.getpid(),
        assertions_completed=True)), flush=True)


if __name__ == '__main__':
    if sys.argv[1] == 'create':
        create(Path(sys.argv[2]), sys.argv[3], sys.argv[4])
    else:
        inspect(json.loads(sys.argv[2]), sys.argv[3])
