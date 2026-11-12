"""LAB ONLY app entrypoint: the runtime.lock part of deploy/nexus/first_run.py.

Nexus first_run.py additionally requires /dev/nvme binds, a RO CIFS media mount
and installation evidence; the laptop lab cannot attest those. This stand-in
keeps the shared /state/runtime.lock held by the single Uvicorn process, so the
original worker runtime_exclusion still detects a running application.
"""
import fcntl
import os
import sys

lock = os.open('/state/runtime.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
try:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    print('STOP: Another instance is running (runtime lock)', file=sys.stderr)
    raise SystemExit(1)
os.set_inheritable(lock, True)
os.execvp(sys.argv[1], sys.argv[1:])
