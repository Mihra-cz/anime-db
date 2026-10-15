"""E5B observes original scratch artifacts; never repairs SQLite or execution."""
from dataclasses import replace
import gc
import os
import sqlite3
import subprocess
import sys

import pytest

from app import target_execution_reconciliation as reconciliation
from app.target_execution_coordination import ScratchActionSession
from app.target_execution_manifest import LocatorPatch
from test_v6_execution_database import sql, reseal_database
from test_v6_execution_filesystem import seal, changed_action
from test_v6_execution_reconciliation import snapshot
from v6_database_case import prepare_database


@pytest.fixture
def case(tmp_path):
    with ScratchActionSession.create(tmp_path) as session:
        manifest, source, target, path = prepare_database(session)
        # Real E3B journal/evidence and physical move, DB remains at old locators.
        session.execute(manifest, manifest.payload.actions[0], action_index=0)
        yield session, manifest, source, target, path


def inspect(case, **overrides):
    from app import target_execution_diagnostics as diagnostics
    session, manifest, _, _, _ = case
    arguments = dict(action_index=0, scratch_root=session.root,
        journal_path=session.journal_path, evidence_path=session.evidence_path)
    arguments.update(overrides)
    before = snapshot(session.root.parent)
    result = diagnostics.inspect_scratch_video_move(manifest, manifest.payload.actions[0], **arguments)
    assert snapshot(session.root.parent) == before
    assert result.outcome == 'STOP' and result.manual_review_required is True
    assert result.identity_continuity == 'UNKNOWN'
    assert result.database.full_post_state == 'UNKNOWN'
    return result


def no_sqlite(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('SQLite connection attempted on uninspectable original')
    monkeypatch.setattr(sqlite3, 'connect', forbidden)


def test_target_only_and_old_locators_are_separate_observations(case):
    result = inspect(case)
    assert result.filesystem.observed_state == 'TARGET_ONLY'
    assert result.filesystem.journal.last_action_phase == 'FS_VERIFIED'
    assert result.database.status == 'DB_INSPECTED'
    assert result.database.locator_state == 'OLD_VALUES'
    assert result.database.video_id == 100
    assert result.database.current == result.database.expected_old
    assert result.database.current.relative_path == 'old/source.mkv'
    assert result.database.current.root_folder == 'old'
    assert result.database.original_fingerprint_match is True
    assert 'TARGET_ONLY_WITH_OLD_VALUES' in result.contradictions
    assert result.manifest_id == case[1].manifest_id
    assert result.action_id == case[1].payload.actions[0].action_id and result.action_index == 0


@pytest.mark.parametrize('source_only', [False, True])
def test_new_locators_do_not_certify_other_columns_or_inode_continuity(case, source_only):
    session, manifest, source, target, path = case
    sql(path, 'UPDATE videos SET relative_path=?,root_folder=?,filename=? WHERE id=100',
        (manifest.payload.actions[0].target, 'Show', 'unrelated changed field'))
    if source_only:
        target.rename(source)
    result = inspect(case)
    assert result.database.locator_state == 'NEW_VALUES'
    assert result.database.current == result.database.expected_new
    assert result.database.original_fingerprint_match is False
    assert result.database.limitations
    if source_only:
        assert 'SOURCE_ONLY_WITH_NEW_VALUES' in result.contradictions


@pytest.mark.parametrize('statement', [
    "UPDATE videos SET root_folder='wrong' WHERE id=100",
    'UPDATE videos SET id=999 WHERE id=100',
    'UPDATE videos SET size=size+1 WHERE id=100',
    'UPDATE videos SET mtime_ns=mtime_ns+1 WHERE id=100',
])
def test_db_conflicts_are_not_interpreted_as_authorized_progress(case, statement):
    sql(case[-1], statement)
    result = inspect(case)
    assert result.database.locator_state == 'MISMATCH'
    assert result.database.contradictions and 'DB_MISMATCH' in result.stop_reasons


@pytest.mark.parametrize('suffix', ['-journal', '-wal', '-shm'])
@pytest.mark.parametrize('kind', ['regular', 'empty', 'symlink'])
def test_any_sidecar_stops_before_sqlite_and_preserves_all_artifacts(case, monkeypatch, suffix, kind):
    path = case[-1]
    sidecar = path.with_name(path.name + suffix)
    if kind == 'symlink':
        sidecar.symlink_to(path)
    else:
        sidecar.write_bytes(b'' if kind == 'empty' else b'preserve recovery artifact')
        sidecar.chmod(0o600)
    no_sqlite(monkeypatch)
    result = inspect(case)
    assert result.database.status == 'DB_UNINSPECTED'
    assert result.database.locator_state == 'UNINSPECTED'
    assert 'SQLITE_SIDECAR' in result.stop_reasons
    assert any(a.suffix == suffix and a.presence == 'PRESENT' for a in result.database.artifacts)
    assert result.database.transaction_state == 'UNKNOWN'


@pytest.mark.parametrize('fault', ['missing', 'empty', 'header', 'wal_header', 'symlink', 'hardlink', 'directory', 'unreadable'])
def test_unsafe_or_unavailable_database_is_never_opened(case, monkeypatch, fault):
    path = case[-1]
    if fault == 'missing':
        path.unlink()
    elif fault == 'symlink':
        other = path.with_suffix('.saved')
        path.rename(other)
        path.symlink_to(other)
    elif fault == 'hardlink':
        os.link(path, path.with_suffix('.linked'))
    elif fault == 'directory':
        path.unlink()
        path.mkdir(mode=0o700)
    elif fault == 'unreadable':
        path.chmod(0)
    elif fault == 'wal_header':
        raw = bytearray(path.read_bytes())
        raw[18:20] = b'\x02\x02'
        path.write_bytes(raw)
    else:
        path.write_bytes(b'' if fault == 'empty' else b'invalid sqlite header')
    no_sqlite(monkeypatch)
    if fault == 'unreadable':
        from app.target_execution_diagnostics import inspect_scratch_video_move
        session, manifest, _, _, _ = case
        before = path.lstat()
        result = inspect_scratch_video_move(manifest, manifest.payload.actions[0], action_index=0,
            scratch_root=session.root, journal_path=session.journal_path, evidence_path=session.evidence_path)
        assert path.lstat() == before
    else:
        result = inspect(case)
    assert result.database.status == 'DB_UNINSPECTED'
    assert result.database.locator_state == 'UNINSPECTED'


def test_valid_header_with_corrupt_sqlite_pages_is_unknown_and_unchanged(case):
    path = case[-1]
    raw = bytearray(path.read_bytes())
    raw[100:4096] = b'X' * (4096 - 100)
    path.write_bytes(raw)
    result = inspect(case)
    assert result.database.locator_state == 'UNKNOWN'
    assert 'SQLITE_READ_FAILED' in result.stop_reasons


@pytest.mark.parametrize('fault', ['missing', 'foreign', 'before', 'after', 'duplicate', 'id', 'no_hash'])
def test_invalid_patches_or_video_binding_cannot_open_sqlite(case, monkeypatch, fault):
    session, manifest, source, target, path = case
    patches = manifest.payload.locator_patches
    if fault == 'missing':
        manifest = seal(manifest, locator_patches=tuple(p for p in patches if p.field != 'relative_path'))
    elif fault == 'foreign':
        manifest = seal(manifest, locator_patches=patches + (LocatorPatch('video', 200, 'root_folder', 'old', 'Show'),))
    elif fault in {'before', 'after'}:
        manifest = seal(manifest, locator_patches=tuple(replace(p, **{fault: 'wrong'}) if p.field == 'relative_path' else p for p in patches))
    elif fault == 'duplicate':
        manifest = seal(manifest, locator_patches=patches + patches[:1])
    elif fault == 'id':
        manifest = seal(manifest, (changed_action(manifest, object_id=200),))
    else:
        manifest = seal(manifest, expected_post_hash=None)
    no_sqlite(monkeypatch)
    result = inspect((session, manifest, source, target, path))
    assert result.database.locator_state == 'UNINSPECTED'


@pytest.mark.parametrize('artifact', ['journal', 'evidence'])
@pytest.mark.parametrize('fault', ['missing', 'invalid'])
def test_untrusted_execution_artifacts_remain_visible_without_repair(case, artifact, fault):
    path = getattr(case[0], artifact + '_path')
    if fault == 'missing':
        path.unlink()
    else:
        path.write_bytes(b'broken frame\n')
    result = inspect(case)
    assert getattr(result.filesystem, artifact).status != 'VALID'
    assert artifact.upper() + '_UNTRUSTED' in result.stop_reasons


@pytest.mark.parametrize('both', [True, False])
def test_ambiguous_filesystem_does_not_choose_a_file(case, both):
    _, _, source, target, _ = case
    if both:
        source.write_bytes(target.read_bytes())
        source.chmod(0o600)
    else:
        target.unlink()
    result = inspect(case)
    assert result.filesystem.observed_state == ('BOTH_PRESENT' if both else 'BOTH_ABSENT')
    assert ('SOURCE_AND_TARGET_PRESENT' if both else 'SOURCE_AND_TARGET_ABSENT') in result.stop_reasons


def test_safe_read_uses_sqlite_locks_and_releases_owned_fds(case, monkeypatch):
    connect, opened = sqlite3.connect, []
    def watched(database, *args, **kwargs):
        assert str(database).endswith('?mode=ro') and kwargs['uri'] is True
        opened.append(str(database))
        return connect(database, *args, **kwargs)
    monkeypatch.setattr(sqlite3, 'connect', watched)
    gc.collect()
    before = len(os.listdir('/proc/self/fd'))
    for _ in range(3):
        assert inspect(case).database.locator_state == 'OLD_VALUES'
    gc.collect()
    assert len(opened) == 3 and len(os.listdir('/proc/self/fd')) == before


@pytest.mark.parametrize('artifact', ['database', 'journal', 'evidence'])
def test_changed_artifacts_revoke_the_db_state_even_if_bytes_and_mtime_are_restored(case, monkeypatch, artifact):
    from app import target_execution_diagnostics as diagnostics
    session, manifest, _, _, path = case
    changed = path if artifact == 'database' else getattr(session, artifact + '_path')
    original = diagnostics._read_values
    def mutate(*args):
        report = original(*args)
        info, raw = changed.stat(), changed.read_bytes()
        changed.write_bytes(raw)
        os.utime(changed, ns=(info.st_atime_ns, info.st_mtime_ns))
        return report
    monkeypatch.setattr(diagnostics, '_read_values', mutate)
    result = diagnostics.inspect_scratch_video_move(manifest, manifest.payload.actions[0], action_index=0,
        scratch_root=session.root, journal_path=session.journal_path, evidence_path=session.evidence_path)
    assert result.database.locator_state == 'UNKNOWN'
    assert 'SQLITE_UNSTABLE' in result.stop_reasons
    assert result.filesystem.observed_state == 'UNKNOWN'
    assert result.filesystem.journal.status == result.filesystem.evidence.status == 'UNTRUSTED'


def test_sidecar_appearing_during_preconnection_checks_prevents_sqlite(case, monkeypatch):
    from app import target_execution_diagnostics as diagnostics
    session, manifest, _, _, path = case
    original, changed = diagnostics.identity._hash_file, []
    def hashed(fd):
        result = original(fd)
        if os.readlink(f'/proc/self/fd/{fd}') == str(path) and not changed:
            changed.append(True)
            path.with_name(path.name + '-journal').write_bytes(b'concurrent sidecar')
        return result
    monkeypatch.setattr(diagnostics.identity, '_hash_file', hashed)
    no_sqlite(monkeypatch)
    result = diagnostics.inspect_scratch_video_move(manifest, manifest.payload.actions[0], action_index=0,
        scratch_root=session.root, journal_path=session.journal_path, evidence_path=session.evidence_path)
    assert result.database.locator_state == 'UNINSPECTED'
    assert result.outcome == 'STOP' and result.manual_review_required
    assert 'SQLITE_SIDECAR' in result.stop_reasons


@pytest.mark.parametrize('fault', ['root_symlink', 'parent_symlink', 'bool_index', 'wrong_index', 'multiple', 'foreign_root', 'foreign_journal'])
def test_unsafe_context_or_action_cannot_open_sqlite(case, monkeypatch, fault):
    session, manifest, source, target, path = case
    overrides = {}
    if fault == 'root_symlink':
        saved = session.root.with_name('saved-root')
        session.root.rename(saved)
        session.root.symlink_to(saved, target_is_directory=True)
    elif fault == 'parent_symlink':
        saved = session.root.parent.with_name(session.root.parent.name + '-saved')
        session.root.parent.rename(saved)
        session.root.parent.symlink_to(saved, target_is_directory=True)
    elif fault in {'bool_index', 'wrong_index'}:
        overrides['action_index'] = False if fault == 'bool_index' else 1
    elif fault == 'multiple':
        other = changed_action(manifest, object_id=200, source='other/video.mkv')
        manifest = seal(manifest, (manifest.payload.actions[0], other))
    elif fault == 'foreign_root':
        # Gate a foreign lexical path, without probing production data.
        manifest = seal(manifest, library_root='/forbidden-e5b-root')
    else:
        overrides['journal_path'] = session.root.parent / 'foreign.journal'
    no_sqlite(monkeypatch)
    result = inspect((session, manifest, source, target, path), **overrides)
    assert result.database.locator_state == 'UNINSPECTED'


def test_unchanged_root_folder_has_no_invented_patch(tmp_path):
    with ScratchActionSession.create(tmp_path) as session:
        manifest, source, target, path = prepare_database(session)
        same_root = session.root / 'Show/release.mkv'
        source.rename(same_root)
        sql(path, "UPDATE videos SET relative_path='Show/release.mkv',root_folder='Show' WHERE id=100")
        action = changed_action(manifest, source='Show/release.mkv')
        patches = (LocatorPatch('video', 100, 'relative_path', 'Show/release.mkv', action.target),)
        manifest = reseal_database(seal(manifest, (action,), locator_patches=patches), path)
        session.execute_video_move(manifest, action, action_index=0)
        result = inspect((session, manifest, same_root, target, path))
        assert result.database.locator_state == 'NEW_VALUES'
        assert result.database.expected_old.root_folder == result.database.expected_new.root_folder == 'Show'
        assert result.filesystem.evidence.status == 'VALID'


def test_missing_root_patch_is_a_db_mismatch_when_original_root_was_different(case):
    session, manifest, source, target, path = case
    manifest = seal(manifest, locator_patches=tuple(p for p in manifest.payload.locator_patches if p.field != 'root_folder'))
    result = inspect((session, manifest, source, target, path))
    assert result.database.locator_state == 'MISMATCH'
    assert 'VIDEO_LOCATORS_OUTSIDE_APPROVED_PATCHES' in result.contradictions


def test_read_transaction_actually_blocks_a_separate_sqlite_writer(case, monkeypatch):
    from app import target_execution_diagnostics as diagnostics
    original = diagnostics._read_values
    def locked_read(*args):
        report = original(*args)
        code = '''import sqlite3, sys
connection = sqlite3.connect(sys.argv[1], timeout=0)
try:
    try:
        connection.execute('BEGIN EXCLUSIVE')
    except sqlite3.OperationalError as exc:
        assert 'locked' in str(exc)
        print('writer_blocked')
    else:
        raise AssertionError('E5B did not retain SQLite locking')
finally:
    connection.close()
'''
        child = subprocess.run([sys.executable, '-Werror::ResourceWarning', '-c', code, str(case[-1])],
            capture_output=True, text=True, timeout=10)
        assert child.returncode == 0 and child.stdout.strip() == 'writer_blocked' and not child.stderr
        return report
    monkeypatch.setattr(diagnostics, '_read_values', locked_read)
    assert inspect(case).database.locator_state == 'OLD_VALUES'


@pytest.mark.parametrize('fault', ['sidecar', 'read_error'])
def test_error_paths_release_owned_descriptors_and_sqlite_connections(case, monkeypatch, fault):
    from app import target_execution_diagnostics as diagnostics
    if fault == 'sidecar':
        case[-1].with_name(case[-1].name + '-journal').write_bytes(b'preserve')
    else:
        def failed(*args):
            raise sqlite3.DatabaseError('injected read failure')
        monkeypatch.setattr(diagnostics, '_read_values', failed)
    gc.collect()
    before = len(os.listdir('/proc/self/fd'))
    for _ in range(3):
        assert inspect(case).database.status == 'DB_UNINSPECTED'
    gc.collect()
    assert len(os.listdir('/proc/self/fd')) == before


def test_uncertain_sidecar_presence_stops_before_sqlite(case, monkeypatch):
    from app import target_execution_diagnostics as diagnostics
    original = os.stat
    def uncertain(path, *args, **kwargs):
        if str(path).endswith('.sqlite-wal'):
            raise PermissionError('cannot establish sidecar absence')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(os, 'stat', uncertain)
    no_sqlite(monkeypatch)
    result = inspect(case)
    assert result.database.locator_state == 'UNINSPECTED'
    assert 'SQLITE_SIDECAR' in result.stop_reasons
    assert next(a for a in result.database.artifacts if a.suffix == '-wal').presence == 'UNKNOWN'


def test_invalid_root_type_returns_stop_without_a_database_path(case, monkeypatch):
    no_sqlite(monkeypatch)
    result = inspect(case, scratch_root=None)
    assert result.database.locator_state == 'UNINSPECTED'


def test_target_locator_owned_by_another_video_is_a_concrete_conflict(case):
    sql(case[-1], 'UPDATE videos SET relative_path=? WHERE id=200', (case[1].payload.actions[0].target,))
    result = inspect(case)
    assert result.database.locator_state == 'MISMATCH'
    assert 'LOCATOR_OWNED_BY_OTHER_VIDEO' in result.contradictions


def test_recorded_complete_never_proves_current_database_locators(tmp_path):
    with ScratchActionSession.create(tmp_path) as session:
        manifest, source, target, path = prepare_database(session)
        session.execute_video_move(manifest, manifest.payload.actions[0], action_index=0)
        sql(path, "UPDATE videos SET relative_path='old/source.mkv',root_folder='old' WHERE id=100")
        result = inspect((session, manifest, source, target, path))
        assert result.filesystem.journal.last_action_phase == 'COMPLETE'
        assert result.database.locator_state == 'OLD_VALUES'
        assert 'RECORDED_DB_PROGRESS_WITH_OLD_VALUES' in result.contradictions
