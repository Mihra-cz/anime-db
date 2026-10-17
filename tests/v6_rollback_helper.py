"""Actual process-death boundaries for the manually approved scratch return."""
import json
import os
from pathlib import Path
import signal
import sys

from app import target_execution_filesystem as fs
from app import target_execution_rollback as rollback
from app.maintenance import ScratchMaintenance
from app.target_execution_manifest import manifest_from_json


def run(manifest_path, boundary):
    manifest = manifest_from_json(manifest_path.read_text())
    root = Path(manifest.payload.library_root)
    database = root.parent / (root.name + '.sqlite')
    journal = root.parent / (root.name + '.journal.jsonl')
    identity = root.parent / (root.name + '.identity.json')
    record = root.parent / (root.name + '.rollback.jsonl')
    args = dict(action_index=0, scratch_root=root, journal_path=journal, evidence_path=identity)
    protected = {p: p.read_bytes() for p in (database, journal, identity)}
    gate = ScratchMaintenance(database, root)
    connections = []

    def scratch_only(event, values):
        if event == 'sqlite3.connect':
            assert str(values[0]).startswith('file:' + str(database)) and str(values[0]).endswith('?mode=ro')
            connections.append(str(values[0]))
        if event == 'open' and isinstance(values[0], (str, bytes)):
            path = os.fsdecode(values[0])
            assert not path.startswith(('/mnt/', '/media/')) and not path.endswith('/data/anime.db')
            if values[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                assert path in {record.name, str(gate.lock_path)}, path
        assert event not in {'os.mkdir', 'os.remove', 'os.rename', 'os.link', 'os.symlink', 'os.truncate'}

    sys.addaudithook(scratch_only)
    proposed = rollback.propose_scratch_video_rollback(manifest, manifest.payload.actions[0], **args)
    approved = rollback.RollbackApproval(proposed, 'RETURN_TARGET_TO_SOURCE',
        rollback.IDENTITY_LIMITATION, 'explicit-synthetic-process-test')
    source, target = root / manifest.payload.actions[0].source, root / manifest.payload.actions[0].target
    rename, calls = fs._rename_noreplace, []

    def reached():
        assert all(p.read_bytes() == raw for p, raw in protected.items())
        states = [json.loads(line)['state'] for line in record.read_text().splitlines()]
        assert states == ['APPROVED', 'ATTEMPT']
        assert source.exists() is (boundary == 'after_rename')
        assert target.exists() is (boundary != 'after_rename')
        print(json.dumps(dict(pid=os.getpid(), boundary=boundary, assertions_completed=True)), flush=True)
        while True:
            signal.pause()

    def renamed(*values):
        calls.append(values)
        if boundary == 'before_rename':
            reached()
        rename(*values)
        if boundary == 'after_rename':
            reached()

    fs._rename_noreplace = renamed
    with gate.maintenance() as owner:
        result = rollback.rollback_scratch_video_move(manifest, manifest.payload.actions[0],
            **args, approval=approved, maintenance_owner=owner)
    assert result.outcome == 'ROLLED_BACK' and len(calls) == 1
    assert source.read_bytes() == b'original scratch video' and not target.exists()
    assert all(p.read_bytes() == raw for p, raw in protected.items())
    assert connections
    print(json.dumps(dict(pid=os.getpid(), assertions_completed=True, outcome=result.outcome)), flush=True)


if __name__ == '__main__':
    run(Path(sys.argv[1]), sys.argv[2])
