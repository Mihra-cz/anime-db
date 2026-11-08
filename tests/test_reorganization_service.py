"""Derived public progress never replaces the original E1/E7 evidence."""
import json
from pathlib import Path
import time

import pytest


def read_status(root):
    return json.loads((root / 'progress.json').read_text())


def states(root):
    return {row['id']: row['state'] for row in read_status(root)['phases']}


def event(phase, completed=0, index=None, total=3):
    result = dict(phase=phase, completed=completed, total=total, remaining=total-completed)
    if index is not None: result['action_index'] = index
    return result


def test_only_confirmed_e7_phases_become_pass(tmp_path):
    from app.reorganization_service import ServiceProgress
    with ServiceProgress(tmp_path) as service:
        assert states(tmp_path) == dict(manifest='WORK', folders='WAIT', rename='WAIT', sqlite='WAIT', cleanup='WAIT', verification='WAIT')
        service.observe(event('E7_PREFLIGHT'))
        assert states(tmp_path)['manifest'] == 'WORK'
        service.observe(event('MANIFEST_CONFIRMED'))
        service.observe(event('FOLDER_TREE_PREPARING'))
        assert states(tmp_path)['folders'] == 'WORK'
        service.observe(event('FOLDER_TREE_CONFIRMED'))
        assert states(tmp_path)['folders'] == 'PASS'
        service.observe(event('FS_APPLIED', index=0))
        assert read_status(tmp_path)['rename_completed'] == 0
        service.observe(event('FS_VERIFIED', index=0))
        service.observe(event('DB_RECONCILED', index=0))
        assert states(tmp_path)['sqlite'] == 'WORK'
        assert 'PASS' != states(tmp_path)['sqlite']
        for index in range(3):
            service.observe(event('FS_VERIFIED', index=index))
            service.observe(event('COMPLETE', completed=index+1, index=index))
        service.observe(event('ACTIONS_CONFIRMED', completed=3))
        assert states(tmp_path)['rename'] == states(tmp_path)['sqlite'] == 'PASS'
        service.observe(event('WAITING_FOR_CLEANUP_APPROVAL', completed=3))
        assert read_status(tmp_path)['waiting_for_operator']
        assert states(tmp_path)['cleanup'] == 'WAIT'
        service.observe(event('E7H_CLEANUP', completed=3))
        assert states(tmp_path)['cleanup'] == 'WORK'
        service.observe(event('CLEANUP_CONFIRMED', completed=3))
        service.observe(event('FINAL_VERIFICATION', completed=3))
        assert states(tmp_path)['cleanup'] == 'PASS'
        assert states(tmp_path)['verification'] == 'WORK'
        service.finish('NORMAL')
    data = read_status(tmp_path)
    assert data['outcome'] == 'NORMAL' and not data['active']
    assert set(states(tmp_path).values()) == {'PASS'}
    assert data['completed'] == data['rename_completed'] == 3


def test_stop_keeps_confirmed_counts_and_redacts_paths(tmp_path):
    from app.reorganization_service import ServiceProgress
    with ServiceProgress(tmp_path) as service:
        service.observe(event('MANIFEST_CONFIRMED'))
        service.observe(event('FOLDER_TREE_CONFIRMED'))
        service.observe(event('FS_VERIFIED', index=0))
        service.observe(event('COMPLETE', completed=1, index=0))
        service.finish('STOP', error=ValueError('secret //NAS/private/file password=hidden'))
    data = read_status(tmp_path)
    assert data['outcome'] == 'STOP' and not data['active']
    assert data['completed'] == data['rename_completed'] == 1
    assert 'STOP' in states(tmp_path).values()
    assert data['last_confirmed']['completed'] == 1
    assert not any(text in json.dumps(data) for text in ('secret', '//NAS', 'password'))


def test_no_terminal_result_does_not_become_pass_or_stop(tmp_path):
    from app.reorganization_service import ServiceProgress
    service = ServiceProgress(tmp_path)
    service.__enter__()
    service.observe(event('E7_PREFLIGHT'))
    service.close()
    data = read_status(tmp_path)
    assert data['outcome'] == 'UNVERIFIED'
    assert states(tmp_path)['manifest'] == 'WORK'


def test_progress_is_bounded_and_not_written_per_action(tmp_path, monkeypatch):
    import os
    from app.reorganization_service import ServiceProgress
    writes = []
    replace = os.replace
    def observed_replace(source, target):
        writes.append(Path(source).stat().st_size)
        return replace(source, target)
    monkeypatch.setattr(os, 'replace', observed_replace)
    before = time.process_time()
    with ServiceProgress(tmp_path, clock=lambda: 1700000000, monotonic=lambda: 0) as service:
        for phase in ('MANIFEST_CONFIRMED', 'FOLDER_TREE_PREPARING', 'FOLDER_TREE_CONFIRMED'):
            service.observe(event(phase, total=3095))
        for index in range(3095):
            for phase in ('PENDING', 'FS_APPLIED', 'FS_VERIFIED', 'DB_RECONCILED', 'COMPLETE'):
                service.observe(event(phase, completed=index+int(phase=='COMPLETE'), index=index, total=3095))
        service.observe(event('ACTIONS_CONFIRMED', completed=3095, total=3095))
        service.observe(event('WAITING_FOR_CLEANUP_APPROVAL', completed=3095, total=3095))
        service.observe(event('E7H_CLEANUP', completed=3095, total=3095))
        service.observe(event('CLEANUP_CONFIRMED', completed=3095, total=3095))
        service.observe(event('FINAL_VERIFICATION', completed=3095, total=3095))
        service.finish('NORMAL')
    elapsed = time.process_time()-before
    assert len(writes) < 20 and max(writes) < 4096
    print(f'summary observer 15475 E1 events: CPU={elapsed:.4f}s writes={len(writes)} bytes={sum(writes)} max={max(writes)}')


def test_requested_public_status_must_be_local_and_outside_media(tmp_path):
    from app.reorganization_service import ServiceProgress
    with pytest.raises(ValueError):
        ServiceProgress(tmp_path, library_root=tmp_path)


def test_killed_worker_leaves_unverified_snapshot_and_stops_heartbeat(tmp_path):
    import subprocess
    import sys
    child = subprocess.Popen([sys.executable, '-B', '-c', '''
import sys, time
import app.reorganization_service as module
module.INTERVAL_SECONDS = 0.05
with module.ServiceProgress(sys.argv[1]):
    time.sleep(30)
''', str(tmp_path)])
    try:
        deadline = time.monotonic() + 10
        while not (tmp_path / 'progress.json').exists():
            assert child.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.01)
        first = read_status(tmp_path)['updated_at']
        while read_status(tmp_path)['updated_at'] == first:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        child.kill(); child.wait(timeout=5)
        final = read_status(tmp_path)
        time.sleep(0.15)
        assert read_status(tmp_path) == final
        assert final['outcome'] == 'UNVERIFIED' and final['active']
        assert 'PASS' not in {row['state'] for row in final['phases']}
        assert 'STOP' not in {row['state'] for row in final['phases']}
    finally:
        if child.poll() is None:
            child.kill(); child.wait(timeout=5)


def test_summary_publication_failure_never_grants_normal(tmp_path, monkeypatch, caplog):
    from app.reorganization_service import ServiceProgress
    import os
    with ServiceProgress(tmp_path) as service:
        def unavailable(*args):
            raise OSError('secret path unavailable')
        monkeypatch.setattr(os, 'replace', unavailable)
        service.observe(event('MANIFEST_CONFIRMED'))
        service.finish('NORMAL')
    assert read_status(tmp_path)['outcome'] == 'UNVERIFIED'
    assert 'secret path' not in caplog.text
