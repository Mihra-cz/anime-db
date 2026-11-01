"""Ordinary E7 rename must not consume video payload bytes or claim a content hash."""
import hashlib
import json
import os
from pathlib import Path

import pytest

from app.target_execution_cleanup import approve_cleanup, read_completed_cleanup
from app.target_execution_guard import NormalOperation, _require_no_evidence
from app.target_execution_manifest import manifest_to_json
from app.tools.reorganize import execute_approved_reorganization
from test_v6_production import production, SHARE
from v6_database_case import database_rows
from test_v6_execution_projection import expected_rows


def test_metadata_rename_cleanup_normal_never_reads_videos(production, monkeypatch):
    owner, database, manifest, runtime = production
    original = database_rows(database)
    root = owner.root
    lock = database.parent / 'runtime.lock'
    lock.write_bytes(b'')
    lock.chmod(0o600)
    owner.close()
    calls = []
    real_pread = os.pread

    def watched(fd, length, offset):
        name = Path(os.readlink(f'/proc/self/fd/{fd}'))
        if name.is_relative_to(root) and name.suffix.lower() in {'.mkv', '.mp4', '.avi', '.m4v'}:
            calls.append((str(name), length))
            pytest.fail('METADATA_RENAME attempted to read video payload')
        return real_pread(fd, length, offset)

    def decide(proposal):
        selected = tuple(candidate.path for candidate in proposal.candidates
            if candidate.path == 'old' or candidate.path.startswith('old/'))
        return approve_cleanup(proposal, selected, actor='operator', reason='approved empty source dirs')

    with monkeypatch.context() as monitoring:
        monitoring.setattr(os, 'pread', watched)
        result = execute_approved_reorganization(database=database, library_root=root,
            share=SHARE, share_root='/', manifest=manifest,
            approved_sha256=hashlib.sha256(manifest_to_json(manifest).encode()).hexdigest(),
            runtime=runtime, runtime_lock=lock, cleanup_decision=decide)
    assert result['state'] == 'NORMAL'
    assert not calls
    assert database_rows(database) == expected_rows(original, manifest.payload.locator_patches)
    assert isinstance(_require_no_evidence(database), NormalOperation)
    for index in range(len(manifest.payload.actions)):
        evidence = json.loads(database.with_suffix(f'.identity-{index}.json').read_text())['evidence']
        assert evidence['version'] == 2
        assert evidence['mode'] == 'METADATA_RENAME' and evidence['content'] is None
    outcome = read_completed_cleanup(database, manifest, root)
    assert outcome.removed
    assert all(entry.content_sha256 is None for entry in outcome.after_snapshot if entry.kind == 'regular')
    assert (root / 'IMPORT/uploading.mkv').is_file()
    assert (root / 'Duplicates').is_dir()


def test_only_explicitly_selected_source_gets_full_content_evidence(production, monkeypatch):
    from app.maintenance import ScratchMaintenance
    owner, database, manifest, runtime = production
    selected = next(action for action in manifest.payload.actions if action.object_id == 100)
    original = (owner.root / selected.source).stat()
    reads = []
    pread = os.pread

    def observed(fd, size, offset):
        value = pread(fd, size, offset)
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == (original.st_dev, original.st_ino):
            reads.append(len(value))
        return value

    monkeypatch.setattr(os, 'pread', observed)
    gate = ScratchMaintenance.existing(database, owner.root)
    with gate.maintenance() as lease:
        owner.execute(manifest, maintenance_owner=lease, cleanup_decision=lambda _: None,
            resume_scanning=True, runtime_evidence=runtime,
            approved_manifest_sha256=hashlib.sha256(manifest_to_json(manifest).encode()).hexdigest(),
            verify_content=(selected.source,))
    assert sum(reads) > 0
    for index, action in enumerate(manifest.payload.actions):
        record = json.loads(database.with_suffix(f'.identity-{index}.json').read_text())['evidence']
        if action == selected:
            assert record['version'] == 1 and 'mode' not in record
            assert record['content']['coverage'] == 'FULL'
        else:
            assert record['version'] == 2 and record['content'] is None


def test_legacy_full_cleanup_remains_readable_without_rewriting_sqlite(tmp_path):
    from dataclasses import asdict
    from app import target_execution_cleanup as cleanup
    from app.maintenance import ScratchMaintenance
    from app.target_execution_sequence import ScratchManifestExecution
    from test_v6_execution_cleanup import prepare
    from test_v6_cleanup_completion_evidence import _rewrite
    with ScratchManifestExecution.create(tmp_path) as owner:
        manifest, database = prepare(owner)
        with ScratchMaintenance(database, owner.root).maintenance() as lease:
            owner.execute(manifest, maintenance_owner=lease,
                verify_content=tuple(action.source for action in manifest.payload.actions),
                cleanup_decision=lambda proposal: approve_cleanup(proposal, ('old/copy',),
                    actor='operator', reason='legacy FULL compatibility fixture'))
        before_database = database.read_bytes()
        rows = [json.loads(line) for line in owner.cleanup_evidence_path.read_text().splitlines()]
        entries = {entry['path']: entry for entry in rows[0]['proposal']['before_snapshot']}
        rows[0]['version'] = 1
        rows[0].pop('mode')
        for row in rows:
            if row['state'] != 'VERIFIED':
                continue
            entries.pop(row['path'])
            parent = row.pop('parent_after')
            entries[parent['path']] = parent
            row.pop('delta_sha256')
            row['snapshot_sha256'] = cleanup._digest([entries[path] for path in sorted(entries)])
        _rewrite(owner.cleanup_evidence_path, rows)
        restored = read_completed_cleanup(database, manifest, owner.root)
        assert restored.mode == 'FULL' and restored.removed == ('old/copy',)
        assert all(entry.content_sha256 is not None for entry in restored.after_snapshot if entry.kind == 'regular')
        assert database.read_bytes() == before_database


@pytest.mark.parametrize(('syscall', 'read_bytes', 'mapped_bytes', 'copy_bytes'), [
    ('read(5</synthetic/Anime/video.mkv>, "data", 64) = 64', 64, 0, 0),
    ('pread64(5</synthetic/Anime/video.mkv>, "data", 64, 0) = 64', 64, 0, 0),
    ('mmap(NULL, 4096, PROT_READ, MAP_PRIVATE, 5</synthetic/Anime/video.mkv>, 0) = 0x1234', 0, 4096, 0),
    ('sendfile(6</tmp/copy>, 5</synthetic/Anime/video.mkv>, NULL, 64) = 64', 0, 0, 64),
])
def test_media_syscall_probe_observes_native_child_access_and_excludes_fixture_copy(
        tmp_path, syscall, read_bytes, mapped_bytes, copy_bytes):
    from v6_media_io_probe import summarize
    parent = tmp_path / 'syscalls.100'
    child = tmp_path / 'syscalls.101'
    parent.write_text('1.0 write(2</log>, "V6_MEDIA_PHASE EXECUTION /synthetic/Anime\\n", 50) = 50\n'
        '3.0 write(2</log>, "V6_MEDIA_PHASE FIXTURE_VIEW_COPY /synthetic/Anime\\n", 50) = 50\n')
    child.write_text('2.0 '+syscall+'\n4.0 read(5</synthetic/Anime/video.mkv>, "fixture", 128) = 128\n')
    result = summarize((parent, child))['/synthetic/Anime']
    assert (result['media_read_bytes'], result['media_mapped_bytes'], result['media_copy_bytes']) == (
        read_bytes, mapped_bytes, copy_bytes)
