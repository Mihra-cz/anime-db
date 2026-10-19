"""E7F: original R0 admission, durable mkdir evidence, then the E7A lifecycle."""
from contextlib import closing
from dataclasses import replace
import errno
import gc
import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app import target_execution_coordination as stops
from app import target_execution_database as database
from app import target_execution_filesystem as fs
from app.maintenance import ScratchMaintenance
from app.database import make_engine
from app.models import Video
from app.physical_layout_service import confirm_physical_layout_choice
from app.target_execution_manifest import build_execution_manifest, manifest_to_json, post_state_hash_from_manifest
from app.target_execution_sequence import ScratchManifestExecution
from app.target_planner_filesystem import inventory_filesystem, plan_library
from app.target_planner_service import load_planner_context, readonly_planner_session
from app.tools.target_plan import database_fingerprint
from test_v6_execution_filesystem import seal
from test_v6_execution_pair import assert_no_case_descriptors
from test_v6_execution_projection import expected_rows, fresh_manifest
from test_v6_execution_sequence import prepare_sequence
from test_physical_naming import title, NOW
from v6_database_case import database_rows


def prepare(owner, existing=0, *, last_keep=True, deep=False):
    _, path = prepare_sequence(owner)
    # Put every video at a source locator before generating the approval;
    # remove the subtitle fixture so this remains the supported Video lane.
    with closing(sqlite3.connect(path)) as db, db:
        for ident, source in db.execute('SELECT id,relative_path FROM videos').fetchall():
            if not source.startswith('old/'):
                target = f'old/video-{ident}.mkv'
                (owner.root / source).rename(owner.root / target)
                db.execute('UPDATE videos SET relative_path=?,root_folder=? WHERE id=?', (target, 'old', ident))
        for source, in db.execute('SELECT relative_path FROM external_subtitles'):
            (owner.root / source).unlink()
        db.execute('DELETE FROM external_subtitle_compatibilities')
        db.execute('DELETE FROM external_subtitles')
        db.execute("UPDATE catalog_collections SET relative_root_path='legacy'")
        db.execute("UPDATE catalog_titles SET relative_root_path='legacy/season'")
    for directory in sorted(owner.root.rglob('*'), key=lambda p: len(p.parts), reverse=True):
        if directory.is_dir() and directory.name != 'old':
            directory.rmdir()
    if last_keep:
        (owner.root / 'seznam-souboru.txt').write_bytes(b'approved inventory')
        (owner.root / 'seznam-souboru.txt').chmod(0o600)
    if deep:
        engine = make_engine(f'sqlite:///{path}')
        try:
            with Session(engine) as db:
                video = db.get(Video, 100)
                extra = title(db, video.catalog_collection, 'Menus', kind='bonus', metadata=False)
                extra.relative_root_path = 'old'
                video.catalog_title = extra
                video.file_type = video.content_type_manual = 'menu'
                video.episode_number_manual_override = None
                db.flush()
                confirm_physical_layout_choice(db, extra, 'extras_menus', now=NOW)
                db.commit()
        finally:
            engine.dispose()
    first = fresh_manifest(owner, path, windows_root=r'C:\Anime')
    for name in first.payload.directories[:existing]:
        (owner.root / name).mkdir(mode=0o700)
    manifest = fresh_manifest(owner, path, windows_root=r'C:\Anime')
    if deep:
        with readonly_planner_session(path) as db:
            context = load_planner_context(db)
        snapshot = inventory_filesystem(owner.root)
        manifest = build_execution_manifest(context, snapshot,
            plan_library(context, snapshot, windows_root=r'C:\Anime'), database_fingerprint(path),
            mount_identity=owner.mount_identity, windows_root=r'C:\Anime',
            criticality_decisions={('video', 100): ('PRIMARY', 'explicit scratch preservation', 'test')})
    assert not manifest.payload.generation_diagnostics
    if last_keep:
        assert manifest.payload.actions[-1].action == 'KEEP'
    gc.collect()
    return manifest, path


def run(owner, manifest, path):
    with ScratchMaintenance(path, owner.root).maintenance() as lease:
        return owner.execute(manifest, maintenance_owner=lease)


def records(owner):
    raw = owner.directory_evidence_path.read_bytes()
    assert raw.endswith(b'\n')
    entries = [json.loads(line) for line in raw.splitlines()]
    previous = None
    for index, entry in enumerate(entries):
        assert entry['sequence'] == index and entry['previous_sha256'] == previous
        body = {key: value for key, value in entry.items() if key != 'sha256'}
        canonical = json.dumps(body, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode()
        assert entry['sha256'] == hashlib.sha256(canonical).hexdigest()
        previous = entry['sha256']
    return entries


def unchanged(owner, manifest, path, original):
    assert database_rows(path) == original
    assert not owner.journal_path.exists()
    assert not any(owner.root.parent.glob(owner.root.name + '.identity-*.json'))
    assert all((owner.root / a.source).is_file() for a in manifest.payload.actions)
    assert_no_case_descriptors(owner)


@pytest.mark.parametrize('existing', [0, 1, 2])
def test_original_manifest_mkdir_move_and_last_keep_reach_exact_post_hash(tmp_path, monkeypatch, existing):
    with ScratchManifestExecution.create(tmp_path) as owner:
        manifest, path = prepare(owner, existing)
        original, immutable = database_rows(path), manifest_to_json(manifest)
        missing = tuple(d for d in manifest.payload.directories if not (owner.root / d).exists())
        mkdir, order = os.mkdir, []
        def created(name, mode=0o777, *, dir_fd=None):
            assert dir_fd is not None and '/' not in name
            assert database_rows(path) == original and not owner.journal_path.exists()
            assert records(owner)[-1]['state'] == 'INTENT'
            assert records(owner)[-1]['path'] == missing[len(order)]
            order.append(missing[len(order)])
            return mkdir(name, mode, dir_fd=dir_fd)
        monkeypatch.setattr(os, 'mkdir', created)
        outcomes = run(owner, manifest, path)
        assert all(o.outcome == 'COMPLETE' for o in outcomes)
        assert tuple(order) == missing
        assert database_rows(path) == expected_rows(original, manifest.payload.locator_patches)
        with readonly_planner_session(path) as session:
            context = load_planner_context(session)
        assert post_state_hash_from_manifest(manifest, context, inventory_filesystem(owner.root)) == manifest.payload.expected_post_hash
        evidence = records(owner)
        assert evidence[0]['manifest_id'] == manifest.manifest_id
        assert evidence[0]['filesystem_hash'] == manifest.payload.filesystem_hash
        assert [r['state'] for r in evidence[1:]] == [s for _ in missing for s in ('INTENT', 'VERIFIED')] + ['READY']
        assert manifest_to_json(manifest) == immutable
        assert_no_case_descriptors(owner)


def test_deeply_nested_sealed_directories_are_prepared_parent_before_child(tmp_path):
    with ScratchManifestExecution.create(tmp_path) as owner:
        manifest, path = prepare(owner, deep=True)
        assert 'Show/Season 01/Extras/Menu' in manifest.payload.directories
        original = database_rows(path)
        assert all(o.outcome == 'COMPLETE' for o in run(owner, manifest, path))
        intents = [r['path'] for r in records(owner) if r['state'] == 'INTENT']
        assert intents == list(manifest.payload.directories)
        assert database_rows(path) == expected_rows(original, manifest.payload.locator_patches)


@pytest.mark.parametrize('fault', ['mkdir', 'mkdir_applied', 'child_fsync', 'parent_fsync', 'evidence_fsync'])
def test_incomplete_preparation_preserves_evidence_and_never_starts_actions(tmp_path, monkeypatch, fault):
    with ScratchManifestExecution.create(tmp_path) as owner:
        manifest, path = prepare(owner)
        original, mkdir, fsync = database_rows(path), os.mkdir, os.fsync
        attempted = []
        def created(name, mode=0o777, *, dir_fd=None):
            attempted.append(name)
            if fault == 'mkdir':
                raise OSError(errno.EACCES, 'injected mkdir failure')
            result = mkdir(name, mode, dir_fd=dir_fd)
            if fault == 'mkdir_applied':
                raise OSError(errno.EIO, 'mkdir result unacknowledged')
            return result
        def synced(fd):
            label = os.readlink(f'/proc/self/fd/{fd}')
            child = owner.root / manifest.payload.directories[0]
            if ((fault == 'child_fsync' and label == str(child))
                    or (fault == 'parent_fsync' and label == str(owner.root))
                    or (fault == 'evidence_fsync' and label == str(owner.directory_evidence_path) and attempted)):
                raise OSError(errno.EIO, 'injected fsync failure')
            return fsync(fd)
        monkeypatch.setattr(os, 'mkdir', created)
        monkeypatch.setattr(os, 'fsync', synced)
        with pytest.raises(stops.ScratchCoordinationStopped):
            run(owner, manifest, path)
        unchanged(owner, manifest, path, original)
        assert records(owner)[-1]['state'] != 'READY'
        saved = owner.directory_evidence_path.read_bytes()
        with pytest.raises(stops.ScratchCoordinationStopped):
            run(owner, manifest, path)
        assert owner.directory_evidence_path.read_bytes() == saved
        assert len(attempted) == 1


@pytest.mark.parametrize('fault', ['write', 'file_fsync', 'parent_fsync'])
def test_unconfirmed_intent_durability_never_allows_mkdir(tmp_path, monkeypatch, fault):
    with ScratchManifestExecution.create(tmp_path) as owner:
        manifest, path = prepare(owner)
        original, write, fsync = database_rows(path), os.write, os.fsync
        failed = []
        def at_intent(fd):
            label = os.readlink(f'/proc/self/fd/{fd}')
            if label != str(owner.directory_evidence_path):
                return False
            return b'"state":"INTENT"' in os.pread(fd, 100000, 0)
        def written(fd, data):
            if fault == 'write' and os.readlink(f'/proc/self/fd/{fd}') == str(owner.directory_evidence_path) and b'"state":"INTENT"' in bytes(data):
                failed.append(True)
                # Preserve an actually truncated diagnostic prefix.
                write(fd, bytes(data[:20]))
                raise OSError(errno.ENOSPC, 'partial intent write')
            return write(fd, data)
        def synced(fd):
            label = os.readlink(f'/proc/self/fd/{fd}')
            intent = (owner.directory_evidence_path.exists()
                and b'"state":"INTENT"' in owner.directory_evidence_path.read_bytes())
            if intent and ((fault == 'file_fsync' and at_intent(fd))
                    or (fault == 'parent_fsync' and label == str(owner.root.parent))):
                failed.append(True)
                raise OSError(errno.EIO, 'intent barrier failed')
            return fsync(fd)
        monkeypatch.setattr(os, 'write', written)
        monkeypatch.setattr(os, 'fsync', synced)
        with pytest.raises(stops.ScratchCoordinationStopped): run(owner, manifest, path)
        assert failed
        unchanged(owner, manifest, path, original)
        assert not (owner.root / manifest.payload.directories[0]).exists()
        assert owner.directory_evidence_path.exists()


@pytest.mark.parametrize('fault', ['new_directory', 'file', 'symlink', 'source', 'db', 'plan_hash', 'filesystem_hash', 'omitted_action', 'directory_set', 'foreign_root', 'foreign_mount'])
def test_sealed_r0_parity_is_required_before_any_directory_evidence_or_mkdir(tmp_path, fault):
    with ScratchManifestExecution.create(tmp_path) as owner:
        manifest, path = prepare(owner)
        name = manifest.payload.directories[0]
        if fault == 'new_directory': (owner.root / name).mkdir(mode=0o700)
        elif fault == 'file': (owner.root / name).write_bytes(b'collision')
        elif fault == 'symlink': (owner.root / name).symlink_to(owner.root / 'old', target_is_directory=True)
        elif fault == 'source': (owner.root / manifest.payload.actions[0].source).write_bytes(b'changed')
        elif fault == 'db':
            with closing(sqlite3.connect(path)) as db, db: db.execute("UPDATE videos SET filename='foreign'")
        elif fault in {'plan_hash', 'filesystem_hash'}: manifest = seal(manifest, **{fault: '0'*64})
        elif fault == 'omitted_action': manifest = seal(manifest, manifest.payload.actions[1:])
        elif fault == 'directory_set':
            from app.target_execution_manifest import _digest
            from dataclasses import asdict
            payload = replace(manifest.payload, directories=manifest.payload.directories + ('unapproved',))
            manifest = replace(manifest, payload=payload, manifest_id=_digest(asdict(payload)))
        elif fault == 'foreign_root': manifest = seal(manifest, library_root='/tmp/unrelated')
        else: manifest = seal(manifest, mount_identity='foreign')
        original = database_rows(path)
        with pytest.raises(stops.ScratchCoordinationStopped): run(owner, manifest, path)
        assert database_rows(path) == original
        assert not owner.directory_evidence_path.exists() and not owner.journal_path.exists()
        assert_no_case_descriptors(owner)


@pytest.mark.parametrize('fault', ['new_file', 'unapproved_directory', 'other_directory_mtime',
    'source_bytes', 'parent_symlink', 'parent_replaced', 'root_replaced', 'mount_table',
    'child_mount', 'evidence_changed', 'maintenance_lost'])
def test_foreign_change_during_preparation_is_stop_before_any_move_or_db_update(tmp_path, monkeypatch, fault):
    from app.target_execution_directories import _DirectoryPreparation
    with ScratchManifestExecution.create(tmp_path) as owner:
        manifest, path = prepare(owner)
        original, append, changed = database_rows(path), _DirectoryPreparation._append, []
        def appended(preparation, state, **values):
            append(preparation, state, **values)
            if state != 'INTENT' or changed:
                return
            changed.append(True)
            if fault == 'new_file': (owner.root / 'foreign.txt').write_bytes(b'foreign')
            elif fault == 'unapproved_directory': (owner.root / 'unapproved').mkdir(mode=0o700)
            elif fault == 'other_directory_mtime':
                source = owner.root / 'old'
                info = source.stat()
                os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns + 1000000))
            elif fault == 'source_bytes':
                source = owner.root / manifest.payload.actions[0].source
                info, raw = source.stat(), source.read_bytes()
                source.write_bytes(raw)
                os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns))
            elif fault in {'parent_symlink', 'parent_replaced'}:
                source = owner.root / 'old'
                info = source.stat()
                detached = owner.root.parent / 'detached'
                source.rename(detached)
                if fault == 'parent_symlink': source.symlink_to(detached, target_is_directory=True)
                else:
                    source.mkdir(mode=0o700)
                    os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns))
            elif fault == 'root_replaced':
                owner.root.rename(owner.root.parent / 'detached')
                owner.root.mkdir(mode=0o700)
            elif fault == 'mount_table':
                read = fs._read_mounts
                monkeypatch.setattr(fs, '_read_mounts', lambda: tuple(reversed(read())))
            elif fault == 'child_mount':
                mount_id = fs._mount_id
                first = str(owner.root / manifest.payload.directories[0])
                monkeypatch.setattr(fs, '_mount_id', lambda fd: -1 if os.readlink(f'/proc/self/fd/{fd}') == first else mount_id(fd))
            elif fault == 'evidence_changed':
                with owner.directory_evidence_path.open('ab') as stream: stream.write(b'foreign\n')
            else: owner._maintenance_owner.close()
        monkeypatch.setattr(_DirectoryPreparation, '_append', appended)
        with pytest.raises(stops.ScratchCoordinationStopped): run(owner, manifest, path)
        assert changed and database_rows(path) == original
        assert not owner.journal_path.exists()
        assert not any(owner.root.parent.glob(owner.root.name + '.identity-*.json'))
        if fault == 'evidence_changed':
            assert not (owner.root / manifest.payload.directories[0]).exists()
        assert_no_case_descriptors(owner)


@pytest.mark.parametrize('fault', ['new_file', 'directory_mtime', 'source_rewrite', 'created_replaced'])
def test_own_mkdir_does_not_normalize_away_a_foreign_change(tmp_path, monkeypatch, fault):
    with ScratchManifestExecution.create(tmp_path) as owner:
        manifest, path = prepare(owner)
        original, mkdir, changed = database_rows(path), os.mkdir, []
        def created(name, mode=0o777, *, dir_fd=None):
            mkdir(name, mode, dir_fd=dir_fd)
            if changed:
                return
            changed.append(True)
            if fault == 'new_file': (owner.root / 'foreign.txt').write_bytes(b'foreign')
            elif fault == 'directory_mtime':
                source = owner.root / 'old'
                info = source.stat()
                os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns + 1000000))
            elif fault == 'source_rewrite':
                source = owner.root / manifest.payload.actions[0].source
                info, raw = source.stat(), source.read_bytes()
                source.write_bytes(raw)
                os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns))
            else:
                # Replacement after the initial child fd is pinned is exercised
                # at its fsync, below; keep the real syscall wrapper minimal.
                pass
        monkeypatch.setattr(os, 'mkdir', created)
        if fault == 'created_replaced':
            fsync = os.fsync
            child = owner.root / manifest.payload.directories[0]
            swapped = []
            def synced(fd):
                if os.readlink(f'/proc/self/fd/{fd}') == str(child) and not swapped:
                    swapped.append(True)
                    info = child.stat()
                    child.rename(owner.root.parent / 'detached')
                    mkdir(child, mode=0o700)
                    os.utime(child, ns=(info.st_atime_ns, info.st_mtime_ns))
                return fsync(fd)
            monkeypatch.setattr(os, 'fsync', synced)
        with pytest.raises(stops.ScratchCoordinationStopped): run(owner, manifest, path)
        unchanged(owner, manifest, path, original)
        assert records(owner)[-1]['state'] == 'INTENT'
        assert (owner.root / manifest.payload.directories[0]).is_dir()


@pytest.mark.parametrize('parent_level', ['root', 'nested'])
def test_parent_mtime_changed_during_fsync_is_not_absorbed_as_own_mkdir(tmp_path, monkeypatch, parent_level):
    with ScratchManifestExecution.create(tmp_path) as owner:
        manifest, path = prepare(owner)
        original, fsync, changed = database_rows(path), os.fsync, []
        child = owner.root / manifest.payload.directories[0 if parent_level == 'root' else 1]
        def synced(fd):
            if not changed and os.readlink(f'/proc/self/fd/{fd}') == str(child):
                changed.append(True)
                info = child.parent.stat()
                os.utime(child.parent, ns=(info.st_atime_ns, info.st_mtime_ns + 1000000))
            return fsync(fd)
        monkeypatch.setattr(os, 'fsync', synced)
        with pytest.raises(stops.ScratchCoordinationStopped): run(owner, manifest, path)
        assert changed
        unchanged(owner, manifest, path, original)
        assert records(owner)[-1]['state'] == 'INTENT'


@pytest.mark.parametrize('fault', ['mkdir', 'parent_symlink', 'parent_replaced', 'foreign_target'])
def test_second_directory_failure_keeps_the_verified_first_and_stops(tmp_path, monkeypatch, fault):
    from app.target_execution_directories import _DirectoryPreparation
    with ScratchManifestExecution.create(tmp_path) as owner:
        manifest, path = prepare(owner)
        original, append, mkdir = database_rows(path), _DirectoryPreparation._append, os.mkdir
        first, second = manifest.payload.directories
        if fault == 'mkdir':
            def created(name, mode=0o777, *, dir_fd=None):
                if name == second.split('/')[-1]:
                    raise OSError(errno.EACCES, 'second mkdir rejected')
                return mkdir(name, mode, dir_fd=dir_fd)
            monkeypatch.setattr(os, 'mkdir', created)
        else:
            def appended(self, state, **values):
                append(self, state, **values)
                if state != 'INTENT' or values['path'] != second:
                    return
                if fault == 'foreign_target':
                    (owner.root / second).mkdir(mode=0o700)
                else:
                    parent = owner.root / first
                    info = parent.stat()
                    detached = owner.root.parent / 'detached'
                    parent.rename(detached)
                    if fault == 'parent_symlink': parent.symlink_to(detached, target_is_directory=True)
                    else:
                        parent.mkdir(mode=0o700)
                        os.utime(parent, ns=(info.st_atime_ns, info.st_mtime_ns))
            monkeypatch.setattr(_DirectoryPreparation, '_append', appended)
        with pytest.raises(stops.ScratchCoordinationStopped): run(owner, manifest, path)
        unchanged(owner, manifest, path, original)
        evidence = records(owner)
        assert [(r['state'], r.get('path')) for r in evidence[1:]] == [
            ('INTENT', first), ('VERIFIED', first), ('INTENT', second)]


@pytest.mark.parametrize('moment', ['before', 'after', 'verified'])
def test_sigkill_preserves_diagnostic_prefix_without_any_action_or_db_commit(tmp_path, moment):
    from app.target_execution_manifest import manifest_from_json
    helper = Path(__file__).with_name('v6_directory_restart_helper.py')
    child = subprocess.run([sys.executable, str(helper), str(tmp_path), moment],
        capture_output=True, text=True, timeout=60)
    assert child.returncode == -signal.SIGKILL, child.stderr
    case = json.loads((tmp_path / 'case.json').read_text())
    manifest = manifest_from_json(case['manifest'])
    root, path = Path(case['root']), Path(case['database'])
    evidence_path = root.parent / (root.name + '.directories.jsonl')
    evidence = [json.loads(line) for line in evidence_path.read_bytes().splitlines()]
    assert evidence[-1]['state'] == ('VERIFIED' if moment == 'verified' else 'INTENT')
    assert not any(r['state'] == 'READY' for r in evidence)
    assert (root / manifest.payload.directories[0]).exists() == (moment != 'before')
    assert not root.with_name(root.name + '.journal.jsonl').exists()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == case['database_sha256']
    assert all((root / action.source).is_file() for action in manifest.payload.actions)


@pytest.mark.parametrize('mkdir_attempted', [False, True])
def test_preparation_stop_preserves_safe_rejection_or_requires_fs_reconciliation(tmp_path, monkeypatch, mkdir_attempted):
    from app.target_execution_directories import _DirectoryPreparation
    with ScratchManifestExecution.create(tmp_path) as owner:
        manifest, path = prepare(owner)
        original, immutable = database_rows(path), manifest_to_json(manifest)
        mkdir, append = os.mkdir, _DirectoryPreparation._append
        rejection = fs.RenameRejected('STOP: safe rejection before mkdir')
        def created(name, mode=0o777, *, dir_fd=None):
            result = mkdir(name, mode, dir_fd=dir_fd)
            other = owner.root / 'old'
            info = other.stat()
            os.utime(other, ns=(info.st_atime_ns, info.st_mtime_ns + 1000000))
            return result
        def appended(self, state, **values):
            append(self, state, **values)
            if state == 'INTENT' and not mkdir_attempted:
                raise rejection
        monkeypatch.setattr(os, 'mkdir', created)
        monkeypatch.setattr(_DirectoryPreparation, '_append', appended)
        with pytest.raises(stops.ScratchCoordinationStopped) as error:
            run(owner, manifest, path)
        assert records(owner)[-1]['state'] == 'INTENT'
        assert (owner.root / manifest.payload.directories[0]).is_dir() == mkdir_attempted
        assert error.value.phase == 'DIRECTORY_PREPARATION'
        assert isinstance(error.value.__cause__, fs.RenameRejected)
        if mkdir_attempted:
            assert type(error.value) is stops.ScratchFilesystemReconciliationRequired
            assert error.value.reconciliation_required is True
            assert 'original R0 plus verified mkdirs' in str(error.value.__cause__)
        else:
            assert type(error.value) is stops.ScratchCoordinationStopped
            assert error.value.reconciliation_required is False
            assert error.value.__cause__ is rejection
        assert manifest_to_json(manifest) == immutable
        unchanged(owner, manifest, path, original)


@pytest.mark.parametrize('existing', [0, 2])
def test_unacknowledged_ready_with_all_directories_present_cannot_start_actions(tmp_path, monkeypatch, existing):
    with ScratchManifestExecution.create(tmp_path) as owner:
        manifest, path = prepare(owner, existing)
        original, fsync = database_rows(path), os.fsync
        def synced(fd):
            if (os.readlink(f'/proc/self/fd/{fd}') == str(owner.directory_evidence_path)
                    and records(owner)[-1]['state'] == 'READY'):
                assert all((owner.root / name).is_dir() for name in manifest.payload.directories)
                raise OSError(errno.EIO, 'READY durability unacknowledged')
            return fsync(fd)
        monkeypatch.setattr(os, 'fsync', synced)
        with pytest.raises(stops.ScratchFilesystemReconciliationRequired) as error:
            run(owner, manifest, path)
        assert error.value.phase == 'DIRECTORY_PREPARATION'
        assert isinstance(error.value.__cause__, OSError)
        assert error.value.__cause__.errno == errno.EIO
        assert records(owner)[-1]['state'] == 'READY'
        assert owner._directories.ready is False
        assert all((owner.root / name).is_dir() for name in manifest.payload.directories)
        unchanged(owner, manifest, path, original)


def test_preparation_stage_zero_rejects_external_db_fingerprint_change_under_writer_lock(tmp_path, monkeypatch):
    from app.target_execution_directories import _DirectoryPreparation
    with ScratchManifestExecution.create(tmp_path) as owner:
        manifest, path = prepare(owner)
        original, raw = database_rows(path), path.read_bytes()
        append, locked = _DirectoryPreparation._append, []
        def appended(self, state, **values):
            append(self, state, **values)
            if state != 'INTENT' or locked:
                return
            # An independent SQL writer cannot change R0 under BEGIN IMMEDIATE.
            # A real metadata change to the adjacent DB file remains possible
            # and invalidates the sealed fingerprint without corrupting SQLite.
            with closing(sqlite3.connect(path, timeout=0)) as other:
                with pytest.raises(sqlite3.OperationalError) as busy:
                    other.execute("UPDATE videos SET filename='unapproved writer'")
                assert busy.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
                locked.append(busy.value.sqlite_errorcode)
            info = path.stat()
            os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1000000))
        monkeypatch.setattr(_DirectoryPreparation, '_append', appended)
        with pytest.raises(stops.ScratchCoordinationStopped) as error:
            run(owner, manifest, path)
        assert locked == [sqlite3.SQLITE_BUSY]
        assert error.value.phase == 'DIRECTORY_PREPARATION'
        assert type(error.value) is stops.ScratchFilesystemReconciliationRequired
        assert isinstance(error.value.__cause__, ValueError)
        assert 'original sealed R0/Rn projection' in str(error.value.__cause__)
        assert path.read_bytes() == raw
        assert path.stat().st_mtime_ns != manifest.payload.db_fingerprint.mtime_ns
        assert records(owner)[-1]['state'] == 'INTENT'
        assert not (owner.root / manifest.payload.directories[0]).exists()
        unchanged(owner, manifest, path, original)


def test_directory_identity_change_after_ready_stops_before_actions(tmp_path, monkeypatch):
    from app.target_execution_directories import _DirectoryPreparation
    with ScratchManifestExecution.create(tmp_path) as owner:
        manifest, path = prepare(owner)
        original, prepare_directories = database_rows(path), _DirectoryPreparation.prepare
        swapped = []
        def prepared(self):
            prepare_directories(self)
            assert self.ready is True and records(owner)[-1]['state'] == 'READY'
            target = owner.root / manifest.payload.directories[-1]
            assert not tuple(target.iterdir())
            before, parent_before = target.stat(), target.parent.stat()
            target.rename(owner.root.parent / 'detached-approved-directory')
            target.mkdir(mode=0o700)
            os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
            os.utime(target.parent, ns=(parent_before.st_atime_ns, parent_before.st_mtime_ns))
            assert target.stat().st_ino != before.st_ino
            # The path/stat snapshot is unchanged; the held directory identity
            # is the guard that must stop this substitution before observation.
            assert inventory_filesystem(owner.root) == self.expected
            swapped.append(True)
        monkeypatch.setattr(_DirectoryPreparation, 'prepare', prepared)
        with pytest.raises(stops.ScratchCoordinationStopped) as error:
            run(owner, manifest, path)
        assert swapped == [True]
        assert isinstance(error.value.__cause__, fs.RenameRejected)
        assert 'parent binding changed' in str(error.value.__cause__)
        assert owner._directories.ready is True
        assert records(owner)[-1]['state'] == 'READY'
        unchanged(owner, manifest, path, original)
