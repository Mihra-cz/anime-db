"""Private-safe diagnostics preserve every exact file-stat checkpoint fence."""
from contextlib import contextmanager, nullcontext
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from app import target_execution_sequence as sequence
from app.target_execution_coordination import ScratchFilesystemReconciliationRequired
from app.target_execution_identity import FileStatEvidence


EXPECTED = FileStatEvidence(101, 202, 303, 404, 505, 'regular', 1, 384, 1000, 1001)
PRIVATE_SOURCE = 'private-source-title/original-video.mkv'
PRIVATE_TARGET = 'private-target-title/renamed-video.mkv'


@pytest.fixture
def checkpoint_owner(tmp_path, monkeypatch):
    """Exercise checkpoint comparisons without a journal, DB, or live library."""
    path = tmp_path / 'private-file.mkv'
    path.write_bytes(b'isolated checkpoint')
    with path.open('rb') as source:
        owner = object.__new__(sequence.ScratchManifestExecution)
        owner._phase = 'DB_PRE_UPDATE'
        owner._active_index = 4
        owner._completed_stats = {}
        owner._active_stats = {4: EXPECTED}
        owner._filesystem = SimpleNamespace(_checkpoint=nullcontext)
        action = SimpleNamespace(action='KEEP', source=PRIVATE_SOURCE, target=PRIVATE_TARGET)
        owner._manifest = SimpleNamespace(payload=SimpleNamespace(actions=(action,) * 5))
        owner._observations = {4: SimpleNamespace(verify=lambda: 'unchanged')}
        owner._verifications = {4: 'unchanged'}
        owner._identity_check = lambda index: None

        @contextmanager
        def target_file(index):
            assert index in {2, 4}
            yield source.fileno()

        owner._target_file = target_file
        monkeypatch.setattr(sequence, '_file_stat', lambda info: EXPECTED)
        yield owner


def assert_diagnostic(error, *, checkpoint, index, phase, differences):
    assert type(error) is ScratchFilesystemReconciliationRequired
    assert error.phase == phase
    assert error.reconciliation_required is True
    marker = '; CHECKPOINT_MISMATCH_JSON='
    assert marker in str(error)
    detail = json.loads(str(error).split(marker, 1)[1])
    assert detail == {
        'checkpoint': checkpoint,
        'action_index': index,
        'active_action_index': 4,
        'phase': phase,
        'differences': differences,
    }
    assert error.checkpoint_mismatch == detail
    assert len(str(error)) < 8192
    for private in (PRIVATE_SOURCE, PRIVATE_TARGET, 'private-file.mkv', 'private-type-value'):
        assert private not in str(error)
        assert private not in repr(detail)


@pytest.mark.parametrize('checkpoint,index,prefix', [
    ('active', 4, 'STOP: active content checkpoint changed'),
    ('completed', 2, 'STOP: completed file checkpoint changed'),
])
@pytest.mark.parametrize('field_name,actual_value,want', [
    ('st_dev', 102, {'st_dev': {'expected': 101, 'actual': 102}}),
    ('st_ino', 203, {'st_ino': {'expected': 202, 'actual': 203}}),
    ('size', 304, {'size': {'expected': 303, 'actual': 304}}),
    ('mtime_ns', 405, {'mtime_ns': {'expected': 404, 'actual': 405}}),
    ('ctime_ns', 506, {'ctime_ns': {'expected': 505, 'actual': 506}}),
    ('file_type', 'private-type-value', {'file_type': {}}),
    ('nlink', 2, {'nlink': {'expected': 1, 'actual': 2}}),
    ('mode', 385, {'mode': {'expected': 384, 'actual': 385}}),
    ('uid', 1002, {'uid': {'expected': 1000, 'actual': 1002}}),
    ('gid', 1003, {'gid': {'expected': 1001, 'actual': 1003}}),
])
def test_each_exact_stat_difference_still_stops_with_private_diagnostic(
        checkpoint_owner, monkeypatch, checkpoint, index, prefix, field_name, actual_value, want):
    owner = checkpoint_owner
    if checkpoint == 'completed':
        owner._completed_stats[index] = EXPECTED
    before_completed = owner._completed_stats.copy()
    before_active = owner._active_stats.copy()
    actual = replace(EXPECTED, **{field_name: actual_value})
    monkeypatch.setattr(sequence, '_file_stat', lambda info: actual)

    with pytest.raises(ScratchFilesystemReconciliationRequired) as stopped:
        owner._verify_physical(index)

    assert str(stopped.value).startswith(prefix + ';')
    assert_diagnostic(stopped.value, checkpoint=checkpoint, index=index,
        phase='DB_PRE_UPDATE', differences=want)
    assert owner._completed_stats == before_completed
    assert owner._active_stats == before_active


def test_completed_post_check_stat_change_has_distinct_diagnostic(checkpoint_owner, monkeypatch):
    owner = checkpoint_owner
    owner._completed_stats[2] = EXPECTED
    observations = iter((EXPECTED, replace(EXPECTED, ctime_ns=506, nlink=2)))
    monkeypatch.setattr(sequence, '_file_stat', lambda info: next(observations))

    with pytest.raises(ScratchFilesystemReconciliationRequired) as stopped:
        owner._verify_physical(2)

    assert str(stopped.value).startswith('STOP: target changed during checkpoint;')
    assert_diagnostic(stopped.value, checkpoint='completed_post_check', index=2,
        phase='DB_PRE_UPDATE', differences={
            'ctime_ns': {'expected': 505, 'actual': 506},
            'nlink': {'expected': 1, 'actual': 2},
        })


def test_active_finalization_stat_change_cannot_create_completed_checkpoint(checkpoint_owner, monkeypatch):
    owner = checkpoint_owner
    owner._phase = 'IDENTITY_VERIFICATION'
    owner._identities = {4: SimpleNamespace(close=lambda: None)}
    monkeypatch.setattr(sequence._ExecutionSequence, '_run_move', lambda self, index: 'finished')
    monkeypatch.setattr(sequence, '_file_stat', lambda info: replace(EXPECTED, ctime_ns=506))

    with pytest.raises(ScratchFilesystemReconciliationRequired) as stopped:
        owner._run_move(4)

    assert str(stopped.value).startswith('STOP: file changed after MOVE identity verification;')
    assert_diagnostic(stopped.value, checkpoint='active_finalize', index=4,
        phase='IDENTITY_VERIFICATION', differences={'ctime_ns': {'expected': 505, 'actual': 506}})
    assert owner._completed_stats == {}


@pytest.mark.parametrize('index', [2, 4])
def test_matching_stat_checkpoint_continues(checkpoint_owner, index):
    owner = checkpoint_owner
    if index == 2:
        owner._completed_stats[index] = EXPECTED
    assert owner._verify_physical(index) is None
