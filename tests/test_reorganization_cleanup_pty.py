"""Real kernel PTY: long explicit live cleanup approvals must not be truncated."""
import errno
import json
import os
from pathlib import Path
import pty
import select
import shlex
import signal
import subprocess
import sys
import time

import pytest

HELPER = Path(__file__).with_name('v6_cleanup_pty_helper.py')


def run_terminal(root, count, *, wrong_sha=False, pipe=False, no_readline=False):
    args = [sys.executable, '-B', str(HELPER), str(root), str(count)]
    if no_readline: args.append('--no-readline')
    pid, master = pty.fork()
    if pid == 0:
        os.environ['TERM'] = 'xterm'
        if pipe:
            command = shlex.join(args) + ' | tee ' + shlex.quote(str(root / 'tee.log'))
            os.execv('/bin/bash', ['bash', '-o', 'pipefail', '-c', command])
        os.execv(args[0], args)
    output = bytearray()
    pending = bytearray()
    selection = None
    sent_selection = sent_sha = False
    status = None
    try:
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            readable, writable, _ = select.select([master], [master] if pending else [], [], 0.1)
            if writable:
                written = os.write(master, pending[:1024]);del pending[:written]
            if readable:
                try: chunk = os.read(master, 65536)
                except OSError as error:
                    if error.errno != errno.EIO: raise
                    chunk = b''
                output.extend(chunk)
                if not chunk: break
            if not sent_selection and b'Approve exact empty-directory paths' in output:
                if pipe or no_readline:
                    pytest.fail('execution reached cleanup without valid terminal/readline admission')
                proposal = json.loads((root / 'state/anime.cleanup-proposal.json').read_text())
                paths = [p['path'] for p in proposal['candidates'] if p['path'].startswith('old/unused-')]
                assert len(paths) == count
                selection = json.dumps(paths)
                pending.extend(selection.encode() + b'\n');sent_selection = True
            if sent_selection and not sent_sha and b'Confirm the cleanup proposal SHA-256:' in output:
                proposal = json.loads((root / 'state/anime.cleanup-proposal.json').read_text())
                pending.extend(('0' * 64 if wrong_sha else proposal['proposal_sha256']).encode() + b'\n')
                sent_sha = True
        else:
            pytest.fail('PTY timed out: ' + repr(bytes(output[-3000:])))
        _, status = os.waitpid(pid, 0)
        assert (root / 'test-outcome.json').exists(), bytes(output[-4000:]).decode(errors='replace')
        return os.waitstatus_to_exitcode(status), json.loads((root / 'test-outcome.json').read_text()), selection
    finally:
        if status is None:
            try: os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError: pass
            os.waitpid(pid, 0)
        os.close(master)


@pytest.mark.parametrize('count', [20, 258])
def test_real_pty_accepts_exact_long_live_cleanup(tmp_path, count):
    code, outcome, selection = run_terminal(tmp_path, count)
    assert code == 0
    assert outcome['readline_loaded']
    assert outcome['result']['state'] == 'NORMAL' and outcome['remaining'] == 0
    assert len(outcome['result']['removed_directories']) == count
    if count >= 258: assert len(selection.encode()) > 4095


def test_real_pty_rejects_wrong_live_proposal_sha(tmp_path):
    code, outcome, _ = run_terminal(tmp_path, 20, wrong_sha=True)
    assert code == 2 and outcome['result']['state'] == 'STOP'
    assert outcome['remaining'] == 20


@pytest.mark.parametrize('fault', ['tee', 'readline'])
def test_terminal_requirements_stop_before_claim(tmp_path, fault):
    code, outcome, _ = run_terminal(tmp_path, 20, pipe=fault == 'tee', no_readline=fault == 'readline')
    assert code == 2
    assert outcome['claims'] == 0 and outcome['result'] is None
    assert outcome['remaining'] == 20


def test_missing_tty_stops_before_claim(tmp_path):
    result = subprocess.run([sys.executable, '-B', str(HELPER), str(tmp_path), '20'],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
    assert result.returncode == 2, result.stderr.decode(errors='replace')
    outcome = json.loads((tmp_path / 'test-outcome.json').read_text())
    assert outcome['claims'] == 0 and outcome['result'] is None
