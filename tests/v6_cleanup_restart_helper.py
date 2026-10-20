"""Crash only a fresh synthetic E7H owner, after its completed E7G manifest."""
import hashlib
import json
import os
from pathlib import Path
import signal
import sys

from app.target_execution_cleanup import _Cleanup, approve_cleanup
from app.target_execution_sequence import ScratchManifestExecution
from test_v6_execution_cleanup import prepare, run


if __name__ == '__main__':
    parent, moment = Path(sys.argv[1]), sys.argv[2]
    with ScratchManifestExecution.create(parent) as owner:
        manifest, path = prepare(owner)
        def decision(proposal):
            (parent / 'case.json').write_text(json.dumps(dict(root=str(owner.root), database=str(path),
                db_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                journal_sha256=hashlib.sha256(owner.journal_path.read_bytes()).hexdigest(),
                files={a.target: (owner.root / a.target).read_bytes().hex() for a in manifest.payload.actions})))
            rmdir, append = os.rmdir, _Cleanup._append
            def removed(*args, **kwargs):
                if moment == 'before': os.kill(os.getpid(), signal.SIGKILL)
                result = rmdir(*args, **kwargs)
                if moment == 'after': os.kill(os.getpid(), signal.SIGKILL)
                return result
            def appended(cleanup, state, **values):
                append(cleanup, state, **values)
                if state == 'VERIFIED' and moment == 'verified':
                    os.kill(os.getpid(), signal.SIGKILL)
            os.rmdir, _Cleanup._append = removed, appended
            return approve_cleanup(proposal, ('old/copy',), actor='operator', reason='explicit crash fixture')
        run(owner, manifest, path, decision)
    raise AssertionError('crash window not reached')
