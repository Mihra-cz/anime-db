"""Reuse E5A's kill/reap and before/after snapshot assertions for E5B."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

import test_v6_execution_database_restart as processes
from test_v6_execution_reconciliation import environment


HELPER = Path(__file__).with_name('v6_diagnostics_restart_helper.py')


@pytest.fixture(autouse=True)
def diagnostics_helper(monkeypatch):
    monkeypatch.setattr(processes, 'HELPER', HELPER)


@pytest.mark.parametrize('boundary', ['before_rename', 'after_rename', 'after_fs_verified',
    'after_commit', 'after_db_reconciled', 'after_complete', 'open_transaction'])
def test_new_process_diagnoses_real_sigkill_without_changing_any_original_artifact(tmp_path, boundary):
    ready = processes.killed(tmp_path, boundary)
    result = processes.inspected(tmp_path, ready)
    assert result['pid'] != os.getpid()
    if boundary == 'open_transaction':
        assert result['sqlite_connections'] == 0
        assert Path(ready['paths']['database'] + '-journal').is_file()


def test_completed_e5a_still_only_produces_a_manual_stop_diagnosis(tmp_path):
    child = subprocess.run([sys.executable, '-Werror::ResourceWarning', str(HELPER),
        'completed', str(tmp_path)], env=environment(), capture_output=True, text=True, timeout=30)
    assert child.returncode == 0 and not child.stderr, child.stdout + child.stderr
    ready = json.loads(child.stdout)
    assert ready['boundary_assertions_completed'] is True
    result = processes.inspected(tmp_path, ready)
    assert result['report']['database']['locator_state'] == 'NEW_VALUES'


def test_early_exit_zero_cannot_fake_successful_e5b_inspection(tmp_path):
    ready = processes.killed(tmp_path, 'after_commit')
    with pytest.raises(AssertionError, match='missing final assertion marker'):
        processes.inspected(tmp_path, ready, 'early_exit')
