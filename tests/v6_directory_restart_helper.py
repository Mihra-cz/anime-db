"""A new E7A process killed at explicit directory-preparation crash windows."""
import hashlib
import json
import os
from pathlib import Path
import signal
import sys

from app.target_execution_directories import _DirectoryPreparation
from app.target_execution_manifest import manifest_to_json
from app.target_execution_sequence import ScratchManifestExecution
from test_v6_execution_directories import prepare, run


if __name__ == '__main__':
    parent, moment = Path(sys.argv[1]), sys.argv[2]
    with ScratchManifestExecution.create(parent) as owner:
        manifest, path = prepare(owner)
        (parent / 'case.json').write_text(json.dumps(dict(root=str(owner.root), database=str(path),
            manifest=manifest_to_json(manifest), database_sha256=hashlib.sha256(path.read_bytes()).hexdigest())))
        mkdir, append = os.mkdir, _DirectoryPreparation._append
        def created(*args, **kwargs):
            if moment == 'before': os.kill(os.getpid(), signal.SIGKILL)
            result = mkdir(*args, **kwargs)
            if moment == 'after': os.kill(os.getpid(), signal.SIGKILL)
            return result
        def appended(self, state, **values):
            append(self, state, **values)
            if state == 'VERIFIED' and moment == 'verified': os.kill(os.getpid(), signal.SIGKILL)
        os.mkdir, _DirectoryPreparation._append = created, appended
        run(owner, manifest, path)
    raise AssertionError('crash window was not reached')
