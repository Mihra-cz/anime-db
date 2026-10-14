"""E4A must diagnose killed scratch processes without writes or identity claims."""
import hashlib
import fcntl
import json
import os
from pathlib import Path
import select
import signal
import stat
import subprocess
import sys

import pytest

HELPER = Path(__file__).with_name('v6_restart_helper.py')
REPO = Path(__file__).resolve().parents[1]


def environment():
    return dict(os.environ, PYTHONPATH=str(REPO), PYTHONDONTWRITEBYTECODE='1',
        ANIME_PATH='/tmp', DATABASE_URL='sqlite://', METADATA_DOWNLOAD_ARTWORK='false')


def killed_context(parent, boundary='before_rename', action='MOVE'):
    child = subprocess.Popen([sys.executable, str(HELPER), 'create', str(parent), boundary, action],
        env=environment(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert select.select([child.stdout], [], [], 20)[0], 'child did not reach boundary'
        line = child.stdout.readline()
        assert line, child.stderr.read()
        ready = json.loads(line)
        assert ready['pid'] == child.pid and ready['boundary'] == boundary
        assert ready['boundary_assertions_completed'] is True
        assert child.poll() is None, 'original process already exited'
        child.kill()
        stdout, stderr = child.communicate(timeout=10)
        assert child.returncode == -signal.SIGKILL, (stdout, stderr)
        assert not stdout and not stderr
        return ready
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=10)


def snapshot(parent):
    result = {}
    for path in [parent, *sorted(parent.rglob('*'))]:
        info = path.lstat()
        # Access times can be updated by the kernel on reads; no restore writes.
        value = (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_uid,
            info.st_gid, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if stat.S_ISREG(info.st_mode):
            value += (hashlib.sha256(path.read_bytes()).hexdigest(),)
        elif stat.S_ISLNK(info.st_mode):
            value += (os.readlink(path),)
        result[str(path.relative_to(parent))] = value
    return result


def inspect(parent, ready, variation='normal'):
    before = snapshot(parent)
    child = subprocess.run([sys.executable, str(HELPER), 'inspect', json.dumps(ready['paths']), variation],
        env=environment(), capture_output=True, text=True, timeout=20)
    assert snapshot(parent) == before, 'inspection changed scratch bytes, paths or metadata'
    assert child.returncode == 0, child.stdout + child.stderr
    assert not child.stderr
    assert child.stdout.strip(), 'inspector missing terminal assertion marker'
    done = json.loads(child.stdout)
    assert done['assertions_completed'] is True, 'inspector exited before its assertions'
    assert done['inspector_pid'] not in {os.getpid(), ready['pid']}
    return done['report']


def locators(ready):
    root = Path(ready['paths']['root'])
    return root / 'old/source.mkv', root / 'new/target.mkv'


@pytest.mark.parametrize('action', ['MOVE', 'QUARANTINE'])
@pytest.mark.parametrize(('boundary', 'phase', 'observed'), [
    ('before_rename', 'PENDING', 'SOURCE_ONLY'),
    ('after_rename', 'PENDING', 'TARGET_ONLY'),
    ('after_applied', 'FS_APPLIED', 'TARGET_ONLY'),
    ('after_verified', 'FS_VERIFIED', 'TARGET_ONLY'),
])
def test_restart_after_actual_process_death_is_read_only_and_never_proves_identity(tmp_path, boundary, phase, observed, action):
    ready = killed_context(tmp_path, boundary, action)
    report = inspect(tmp_path, ready)
    assert report['observed_state'] == observed
    assert report['journal']['status'] == 'VALID'
    assert report['journal']['manifest_binding_valid'] is True
    assert report['journal']['last_action_phase'] == phase
    assert report['journal']['history_completeness'] == 'UNKNOWN'
    assert report['evidence']['status'] == 'VALID'
    assert report['evidence']['capture_acknowledgement'] == 'UNKNOWN'
    present = report['source'] if observed == 'SOURCE_ONLY' else report['target']
    assert present['presence'] == 'PRESENT'
    assert present['content_match'] == 'MATCH'
    assert present['filesystem_match'] == 'MATCH'
    assert present['physical_identity_match'] == 'UNKNOWN'
    assert present['observed_content']['digest'] == '9b7080928611c26681c9b3ec3c9067065117368b09c737d72fda1219da79b90f'
    assert 'IDENTITY_CONTINUITY_UNPROVEN' in report['stop_reasons']
    if boundary == 'after_rename':
        assert 'PENDING_WITH_TARGET_ONLY' in report['contradictions']


@pytest.mark.parametrize(('change', 'observed', 'reason'), [
    ('both', 'BOTH_PRESENT', 'SOURCE_AND_TARGET_PRESENT'),
    ('neither', 'BOTH_ABSENT', 'SOURCE_AND_TARGET_ABSENT'),
    ('changed', 'SOURCE_ONLY', 'CONTENT_MISMATCH'),
    ('metadata', 'SOURCE_ONLY', 'METADATA_MISMATCH'),
    ('replacement', 'SOURCE_ONLY', 'METADATA_MISMATCH'),
])
def test_ambiguous_or_contradictory_files_stop_with_specific_evidence(tmp_path, change, observed, reason):
    ready = killed_context(tmp_path)
    source, target = locators(ready)
    if change == 'both':
        target.write_bytes(source.read_bytes())
        target.chmod(0o600)
    elif change == 'neither':
        source.unlink()
    elif change == 'changed':
        info = source.stat()
        source.write_bytes(b'X' * info.st_size)
        os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns))
    elif change == 'metadata':
        info = source.stat()
        os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns + 1000000))
    else:
        # Keep the original inode allocated so the copy cannot reuse its number.
        held = source.with_name('original-held.mkv')
        source.rename(held)
        source.write_bytes(held.read_bytes())
        source.chmod(0o600)
        info = held.stat()
        os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns))
    report = inspect(tmp_path, ready)
    assert report['observed_state'] == observed
    assert reason in report['stop_reasons']
    if change == 'changed':
        assert report['source']['content_match'] == 'MISMATCH'
        assert report['source']['filesystem_match'] == 'MATCH'
    elif change == 'replacement':
        assert report['source']['content_match'] == 'MATCH'
        assert report['source']['filesystem_match'] == 'MISMATCH'
        assert report['source']['physical_identity_match'] == 'UNKNOWN'


@pytest.mark.parametrize('corruption', ['partial', 'partial_first', 'checksum', 'foreign', 'empty', 'header_only', 'whole_frame_loss'])
def test_journal_preserves_only_validated_prefix_and_never_claims_complete_history(tmp_path, corruption):
    ready = killed_context(tmp_path, 'after_verified')
    path = Path(ready['paths']['journal'])
    lines = path.read_bytes().splitlines(keepends=True)
    if corruption == 'partial':
        path.write_bytes(b''.join(lines[:-1]) + lines[-1][:20])
    elif corruption == 'partial_first':
        path.write_bytes(lines[0] + lines[1][:20])
    elif corruption == 'checksum':
        path.write_bytes(b''.join(lines[:-1]) + lines[-1].replace(b'FS_VERIFIED', b'FS_APPLIED'))
    elif corruption == 'foreign':
        header = json.loads(lines[0])
        header['manifest_id'] = '0' * 64
        body = {k: v for k, v in header.items() if k != 'sha256'}
        canonical = lambda v: json.dumps(v, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
        header['sha256'] = hashlib.sha256(canonical(body).encode()).hexdigest()
        path.write_bytes((canonical(header) + '\n').encode())
    elif corruption == 'empty':
        path.write_bytes(b'')
    elif corruption == 'header_only':
        path.write_bytes(lines[0])
    else:
        path.write_bytes(b''.join(lines[:-1]))
    report = inspect(tmp_path, ready)
    expected = 'VALID' if corruption in {'header_only', 'whole_frame_loss'} else 'INVALID'
    assert report['journal']['status'] == expected
    assert report['journal']['history_completeness'] == 'UNKNOWN'
    if corruption in {'partial', 'checksum', 'whole_frame_loss'}:
        assert report['journal']['last_action_phase'] == 'FS_APPLIED'
        assert report['journal']['manifest_binding_valid'] is True
    else:
        assert report['journal']['last_action_phase'] is None
    if corruption == 'partial_first':
        assert report['journal']['manifest_binding_valid'] is True
    assert report['observed_state'] == 'TARGET_ONLY'


@pytest.mark.parametrize('artifact', ['journal', 'evidence'])
@pytest.mark.parametrize('damage', ['missing', 'corrupt', 'symlink', 'hardlink', 'directory', 'fifo'])
def test_unavailable_or_unsafe_artifacts_stop_without_losing_filesystem_diagnostics(tmp_path, artifact, damage):
    ready = killed_context(tmp_path)
    path = Path(ready['paths'][artifact])
    if damage == 'corrupt':
        path.write_bytes(b'{incomplete')
    else:
        original = path.with_suffix('.held')
        path.rename(original)
        if damage == 'symlink':
            path.symlink_to(original)
        elif damage == 'hardlink':
            os.link(original, path)
        elif damage == 'directory':
            path.mkdir()
        elif damage == 'fifo':
            os.mkfifo(path, 0o600)
    report = inspect(tmp_path, ready)
    assert report[artifact]['status'] != 'VALID'
    assert artifact.upper() + '_UNTRUSTED' in report['stop_reasons']
    assert report['observed_state'] == 'SOURCE_ONLY'
    assert report['source']['presence'] == 'PRESENT'
    assert report['source']['physical_identity_match'] == 'UNKNOWN'


@pytest.mark.parametrize('variation', ['other_manifest', 'other_action', 'other_index', 'bool_index', 'foreign_root', 'invalid_manifest'])
def test_wrong_manifest_action_or_index_never_grants_a_bound_observation(tmp_path, variation):
    ready = killed_context(tmp_path)
    report = inspect(tmp_path, ready, variation)
    assert report['evidence']['status'] != 'VALID'
    assert report['stop_reasons']
    assert report['identity_continuity'] == 'UNKNOWN'


@pytest.mark.parametrize('location', ['source', 'target'])
@pytest.mark.parametrize('unsafe', ['symlink', 'dangling_symlink', 'fifo', 'directory', 'hardlink', 'writable'])
def test_unsafe_locator_is_never_hashed_and_other_locator_is_still_reported(tmp_path, location, unsafe):
    ready = killed_context(tmp_path)
    source, target = locators(ready)
    path = source if location == 'source' else target
    if location == 'source':
        source.rename(source.with_suffix('.held'))
    if unsafe == 'symlink':
        path.symlink_to('/mnt/nas-anime/must-never-open.mkv')
    elif unsafe == 'dangling_symlink':
        path.symlink_to(path.parent / 'missing')
    elif unsafe == 'fifo':
        os.mkfifo(path, 0o600)
    elif unsafe == 'directory':
        path.mkdir()
    elif unsafe == 'hardlink':
        original = source if source.exists() else source.with_suffix('.held')
        os.link(original, path)
    else:
        path.write_bytes(b'unsafe')
        path.chmod(0o666)
    report = inspect(tmp_path, ready)
    assert report[location]['presence'] == 'UNSAFE'
    assert report[location]['observed_file'] is not None
    assert report[location]['observed_content'] is None
    assert 'UNSAFE_LOCATOR' in report['stop_reasons']


def test_record_with_recomputed_checksum_still_requires_exact_action_binding(tmp_path):
    ready = killed_context(tmp_path)
    path = Path(ready['paths']['evidence'])
    record = json.loads(path.read_bytes())
    record['evidence']['action_index'] = 1
    canonical = lambda v: json.dumps(v, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
    record['sha256'] = hashlib.sha256(canonical(record['evidence']).encode()).hexdigest()
    path.write_bytes((canonical(record) + '\n').encode())
    report = inspect(tmp_path, ready)
    assert report['evidence']['status'] == 'INVALID'
    assert 'EVIDENCE_UNTRUSTED' in report['stop_reasons']


def test_source_only_contradicts_recorded_filesystem_application(tmp_path):
    ready = killed_context(tmp_path, 'after_applied')
    source, target = locators(ready)
    target.rename(source)
    report = inspect(tmp_path, ready)
    assert report['observed_state'] == 'SOURCE_ONLY'
    assert 'RECORDED_FS_PROGRESS_WITHOUT_TARGET' in report['contradictions']


@pytest.mark.parametrize('artifact', ['journal', 'evidence'])
def test_inspection_never_reads_through_an_active_exclusive_artifact_writer(tmp_path, artifact):
    ready = killed_context(tmp_path)
    with open(ready['paths'][artifact], 'rb') as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        report = inspect(tmp_path, ready)
    assert report[artifact]['status'] == 'UNAVAILABLE'
    assert report['observed_state'] == 'SOURCE_ONLY'
    assert artifact.upper() + '_UNTRUSTED' in report['stop_reasons']


@pytest.mark.parametrize('part', ['root', 'source_parent', 'target_parent'])
def test_symlinked_context_or_parent_is_rejected_without_following_it(tmp_path, part):
    ready = killed_context(tmp_path)
    root = Path(ready['paths']['root'])
    path = root if part == 'root' else root / ('old' if part == 'source_parent' else 'new')
    path.rename(path.with_name(path.name + '-held'))
    path.symlink_to('/mnt/nas-anime/must-never-open', target_is_directory=True)
    report = inspect(tmp_path, ready)
    assert report['observed_state'] == 'UNKNOWN'
    if part == 'root':
        assert report['source'] is None and report['target'] is None
    else:
        item = report['source'] if part == 'source_parent' else report['target']
        assert item['presence'] == 'UNKNOWN' and item['observed_content'] is None


@pytest.mark.parametrize('input_path', ['root', 'journal', 'evidence'])
def test_foreign_paths_are_rejected_before_any_production_open(tmp_path, input_path):
    ready = killed_context(tmp_path)
    ready['paths'][input_path] = '/mnt/nas-anime/must-never-open'
    report = inspect(tmp_path, ready)
    assert 'INSPECTION_UNCERTAIN' in report['stop_reasons']
    assert report['source'] is None and report['target'] is None


def test_subprocess_harness_rejects_zero_exit_before_assertions(tmp_path):
    ready = killed_context(tmp_path)
    with pytest.raises(AssertionError, match='missing terminal assertion marker'):
        inspect(tmp_path, ready, 'early_exit')
