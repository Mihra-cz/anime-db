"""A new process diagnoses E5A after real SIGKILL, without adopting a writer."""
import json
from pathlib import Path
import select
import signal
import subprocess
import sys

import pytest

from test_v6_execution_reconciliation import environment, snapshot


HELPER = Path(__file__).with_name('v6_database_restart_helper.py')


def killed(parent, boundary):
    child = subprocess.Popen([sys.executable, '-Werror::ResourceWarning', str(HELPER),
        'create', str(parent), boundary], env=environment(), stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True)
    try:
        assert select.select([child.stdout], [], [], 20)[0], 'child never reached crash boundary'
        line = child.stdout.readline()
        assert line, child.stderr.read()
        ready = json.loads(line)
        assert ready['boundary_assertions_completed'] is True
        assert ready['pid'] == child.pid and ready['boundary'] == boundary and child.poll() is None
        child.kill()
        stdout, stderr = child.communicate(timeout=10)
        assert child.returncode == -signal.SIGKILL and not stdout and not stderr
        return ready
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=10)


def inspected(parent, ready, variation='normal'):
    before = snapshot(parent)
    completed = subprocess.run([sys.executable, '-Werror::ResourceWarning', str(HELPER), 'inspect',
        json.dumps(ready['paths']), ready['boundary'], variation], env=environment(), capture_output=True,
        text=True, timeout=20)
    assert snapshot(parent) == before, 'restart inspection changed bytes, paths or metadata'
    assert completed.returncode == 0 and not completed.stderr, completed.stdout + completed.stderr
    assert completed.stdout.strip(), 'missing final assertion marker'
    result = json.loads(completed.stdout)
    assert result['assertions_completed'] is True and result['pid'] != ready['pid']
    return result


@pytest.mark.parametrize('boundary', ['after_fs_verified', 'after_commit', 'after_db_reconciled'])
def test_actual_process_death_preserves_partial_state_for_read_only_inspection(tmp_path, boundary):
    ready = killed(tmp_path, boundary)
    result = inspected(tmp_path, ready)
    assert result['report']['outcome'] == 'STOP'
    assert result['report']['identity_continuity'] == 'UNKNOWN'


def test_zero_exit_before_assertions_is_not_a_restart_test_pass(tmp_path):
    ready = killed(tmp_path, 'after_commit')
    with pytest.raises(AssertionError, match='missing final assertion marker'):
        inspected(tmp_path, ready, 'early_exit')
