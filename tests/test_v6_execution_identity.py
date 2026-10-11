"""E3A uses only fresh E2A trees and evidence inside pytest tmp_path.

Faults model protocol decisions, not NAS/ARM64 or power-loss durability.
"""
from dataclasses import asdict, replace
import errno
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path

import pytest

from app import target_execution_filesystem as fs
from app.target_execution_manifest import manifest_to_json
from test_v6_execution_filesystem import prepare, seal, changed_action


def api():
    assert importlib.util.find_spec('app.target_execution_identity') is not None, \
        'E3A scratch durable identity evidence is missing'
    return importlib.import_module('app.target_execution_identity')


@pytest.fixture
def case(tmp_path):
    with api().ScratchIdentitySession.create(tmp_path) as session:
        yield session, *prepare(session)


def capture(session, manifest):
    return session.capture(manifest, manifest.payload.actions[0], action_index=0)


def load(session, manifest):
    return session.load(manifest, manifest.payload.actions[0], action_index=0)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)


@pytest.mark.parametrize('action', ['MOVE', 'QUARANTINE'])
def test_capture_persists_exact_identity_content_and_binding(tmp_path, monkeypatch, action):
    with api().ScratchIdentitySession.create(tmp_path) as session:
        manifest, source, target = prepare(session, action=action)
        original_manifest, before = manifest_to_json(manifest), source.stat()
        monkeypatch.setattr(fs, '_rename_noreplace', lambda *args: pytest.fail('E3A renamed'))
        record = capture(session, manifest)
        assert record.schema == 'animedb-v6-file-identity' and record.version == 1
        assert record.manifest_id == manifest.manifest_id
        assert record.manifest_sha256 == hashlib.sha256(original_manifest.encode()).hexdigest()
        assert record.action == manifest.payload.actions[0] and record.action_index == 0
        assert record.filesystem.st_dev == before.st_dev and record.filesystem.st_ino == before.st_ino
        assert (record.filesystem.size, record.filesystem.mtime_ns, record.filesystem.nlink) == (22, before.st_mtime_ns, 1)
        assert record.filesystem.file_type == 'regular'
        assert record.content.algorithm == 'sha256' and record.content.coverage == 'FULL'
        assert record.content.bytes_hashed == 22
        assert record.content.digest == '9b7080928611c26681c9b3ec3c9067065117368b09c737d72fda1219da79b90f'
        assert record.binding.root == str(session.root)
        assert record.binding.mount_id == session._filesystem._mount_id
        raw = session.evidence_path.read_bytes()
        frame = json.loads(raw)
        assert set(frame) == {'evidence', 'sha256'}
        assert frame['sha256'] == hashlib.sha256(canonical(frame['evidence']).encode()).hexdigest()
        assert raw == (canonical(frame) + '\n').encode()
        assert load(session, manifest) == record
        assert session.evidence_path.parent == session.root.parent
        assert not session.evidence_path.is_relative_to(session.root)
        assert source.read_bytes() == b'original scratch video' and not target.exists()
        assert manifest_to_json(manifest) == original_manifest
        assert not list(session.root.parent.glob('*.journal.jsonl'))


def test_capture_acknowledges_only_after_file_and_parent_barriers(case, monkeypatch):
    session, manifest, source, _ = case
    sync, events = os.fsync, []
    def observe_sync(fd):
        sync(fd)
        events.append(os.readlink(f'/proc/self/fd/{fd}'))
    monkeypatch.setattr(os, 'fsync', observe_sync)
    capture(session, manifest)
    assert events == [str(session.evidence_path), str(session.root.parent)]
    assert source.exists()


@pytest.mark.parametrize('existing', ['file', 'symlink', 'hardlink', 'directory'])
def test_capture_never_overwrites_existing_evidence(case, existing):
    session, manifest, source, target = case
    path = session.evidence_path
    if existing == 'file':
        path.write_bytes(b'keep evidence')
    elif existing == 'symlink':
        path.symlink_to(source)
    elif existing == 'hardlink':
        os.link(source, path)
    else:
        path.mkdir()
    before = path.lstat()
    with pytest.raises(api().IdentityEvidenceRejected):
        capture(session, manifest)
    assert path.lstat() == before
    assert source.read_bytes() == b'original scratch video' and not target.exists()


def test_capture_checks_artifact_parent_mount_before_creation(case, monkeypatch):
    session, manifest, source, target = case
    identity = api()
    open_parent, mount_id = identity.journal._open_parent, fs._mount_id
    parents = []
    def opened(*args):
        fd = open_parent(*args)
        parents.append(fd)
        return fd
    def changed_mount(fd):
        return mount_id(fd) + 1 if fd in parents else mount_id(fd)
    with monkeypatch.context() as injected:
        injected.setattr(identity.journal, '_open_parent', opened)
        injected.setattr(fs, '_mount_id', changed_mount)
        with pytest.raises(identity.IdentityEvidenceRejected):
            capture(session, manifest)
    assert not session.evidence_path.exists()
    assert source.read_bytes() == b'original scratch video' and not target.exists()
    assert session._poisoned and not session._durable
    with pytest.raises(identity.IdentityEvidenceRejected):
        capture(session, manifest)


@pytest.mark.parametrize('fault', ['corrupt', 'read_io', 'binding', 'interrupt', 'exit'])
def test_failed_load_blocks_later_match_even_if_bytes_are_restored(case, monkeypatch, fault):
    session, manifest, source, target = case
    identity = api()
    record = capture(session, manifest)
    raw = session.evidence_path.read_bytes()
    primary = KeyboardInterrupt() if fault == 'interrupt' else SystemExit(143) if fault == 'exit' else None
    selected = replace(manifest, manifest_id='0' * 64) if fault == 'binding' else manifest
    with monkeypatch.context() as injected:
        if fault == 'corrupt':
            session.evidence_path.write_bytes(raw[:-1])
        elif fault in {'read_io', 'interrupt', 'exit'}:
            def fail_read(*args):
                raise primary if primary is not None else OSError(errno.EIO, 'injected load error')
            injected.setattr(os, 'pread', fail_read)
        with pytest.raises(type(primary) if primary is not None else identity.IdentityEvidenceRejected) as caught:
            load(session, selected)
        if primary is not None:
            assert caught.value is primary
            if fault == 'exit':
                assert caught.value.code == 143
    # Manual restoration in this test must not erase the observed STOP.
    if fault == 'corrupt':
        session.evidence_path.write_bytes(raw)
    monkeypatch.setattr(identity, '_hash_file', lambda *args: pytest.fail('retried file comparison'))
    assert session.compare(manifest, record.action, action_index=0).outcome == 'UNKNOWN'
    assert session._poisoned and session._filesystem._poisoned
    assert source.read_bytes() == b'original scratch video' and not target.exists()


@pytest.mark.parametrize('change', ['manifest_id', 'action_id', 'action_payload', 'action_index', 'bool_index', 'foreign_root', 'envelope'])
def test_load_rejects_wrong_immutable_binding(case, change):
    session, manifest, _, _ = case
    capture(session, manifest)
    action, index = manifest.payload.actions[0], 0
    if change == 'manifest_id':
        manifest = replace(manifest, manifest_id='0' * 64)
    elif change == 'action_id':
        action = replace(action, action_id='0' * 64)
    elif change == 'action_payload':
        action = replace(action, criticality_reason='changed authority')
    elif change == 'action_index':
        index = 1
    elif change == 'bool_index':
        index = False
    elif change == 'foreign_root':
        manifest = seal(manifest, library_root='/mnt/nas-anime')
    else:
        manifest = replace(manifest, created_at='2026-10-12T00:00:00Z')
    raw = session.evidence_path.read_bytes()
    with pytest.raises(api().IdentityEvidenceRejected):
        session.load(manifest, action, action_index=index)
    assert session.evidence_path.read_bytes() == raw


@pytest.mark.parametrize('corruption', ['truncate', 'checksum', 'extra_field', 'duplicate_key', 'noncanonical', 'version', 'partial_coverage', 'bool_size'])
def test_strict_load_detects_corrupt_or_incomplete_record_without_repair(case, corruption):
    session, manifest, _, _ = case
    capture(session, manifest)
    raw = session.evidence_path.read_bytes()
    frame = json.loads(raw)
    if corruption == 'truncate':
        raw = raw[:-1]
    elif corruption == 'checksum':
        frame['sha256'] = '0' * 64
        raw = (canonical(frame) + '\n').encode()
    elif corruption == 'duplicate_key':
        raw = raw.replace(b'{', b'{"sha256":"duplicate",', 1)
    elif corruption == 'noncanonical':
        raw = (json.dumps(frame, indent=2) + '\n').encode()
    else:
        body = frame['evidence']
        if corruption == 'extra_field':
            body['execution_authorized'] = True
        elif corruption == 'version':
            body['version'] = 2
        elif corruption == 'partial_coverage':
            body['content']['coverage'] = 'PARTIAL'
        else:
            body['filesystem']['size'] = True
        frame['sha256'] = hashlib.sha256(canonical(body).encode()).hexdigest()
        raw = (canonical(frame) + '\n').encode()
    session.evidence_path.write_bytes(raw)
    with pytest.raises(api().IdentityEvidenceRejected):
        load(session, manifest)
    assert session.evidence_path.read_bytes() == raw


def test_factory_rejects_existing_library_and_no_existing_root_adoption(tmp_path):
    library = tmp_path / 'existing-library'
    library.mkdir(mode=0o700)
    (library/'video.mkv').write_bytes(b'preserve')
    with pytest.raises(TypeError):
        api().ScratchIdentitySession(library)
    with pytest.raises(TypeError):
        api().ScratchIdentitySession.create(tmp_path, root=library)
    with pytest.raises((api().IdentityEvidenceRejected, fs.RenameRejected)):
        api().ScratchIdentitySession.create(Path('/mnt/nas-anime'))
    assert (library/'video.mkv').read_bytes() == b'preserve'


def compare(session, manifest):
    return session.compare(manifest, manifest.payload.actions[0], action_index=0)


@pytest.mark.parametrize('moved', [False, True])
def test_live_comparison_matches_real_source_or_renamed_inode(case, monkeypatch, moved):
    session, manifest, source, target = case
    capture(session, manifest)
    if moved:
        session._filesystem.rename(manifest, manifest.payload.actions[0])
    raw = session.evidence_path.read_bytes()
    monkeypatch.setattr(fs, '_rename_noreplace', lambda *args: pytest.fail('comparison renamed'))
    monkeypatch.setattr(os, 'fsync', lambda *args: pytest.fail('comparison synchronized'))
    result = compare(session, manifest)
    assert result.outcome == 'MATCH'
    present, absent = (result.target, result.source) if moved else (result.source, result.target)
    assert present.presence == 'PRESENT' and absent.presence == 'ABSENT'
    assert present.content_match == present.filesystem_match == present.physical_identity_match == 'MATCH'
    assert session.evidence_path.read_bytes() == raw
    assert source.exists() is not moved and target.exists() is moved


@pytest.mark.parametrize('lost_pin', [False, True])
@pytest.mark.parametrize('state,want', [('both', 'UNKNOWN'), ('neither', 'UNKNOWN'),
    ('different_content', 'MISMATCH'), ('same_content_other_inode', 'MISMATCH'), ('different_mtime', 'MISMATCH')])
def test_locator_content_and_identity_diagnostics(case, lost_pin, state, want):
    session, manifest, source, target = case
    capture(session, manifest)
    before = source.stat()
    if state == 'both':
        target.write_bytes(source.read_bytes())
        target.chmod(0o600)
    elif state == 'neither':
        source.unlink()
    elif state == 'same_content_other_inode':
        target.write_bytes(source.read_bytes())
        target.chmod(0o600)
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        source.unlink()
    else:
        session._filesystem.rename(manifest, manifest.payload.actions[0])
        if state == 'different_content':
            target.write_bytes(b'X' * before.st_size)
            os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        else:
            os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns + 1))
    if lost_pin:
        session.close()  # deterministic loss of live file/parent proof, no restart API
    result = compare(session, manifest)
    expected = 'UNKNOWN' if lost_pin and state == 'same_content_other_inode' else want
    assert result.outcome == expected
    if state == 'different_content':
        assert result.target.content_match == 'MISMATCH'
        assert result.target.observed_file.size == before.st_size
        assert result.target.observed_file.mtime_ns == before.st_mtime_ns
    if state == 'same_content_other_inode':
        assert result.target is not None, result.diagnostics
        assert result.target.content_match == 'MATCH'
        assert result.target.filesystem_match == 'MISMATCH'
        assert result.target.physical_identity_match == ('UNKNOWN' if lost_pin else 'MISMATCH')


@pytest.mark.parametrize('moved', [False, True])
def test_matching_bytes_and_numeric_inode_after_live_evidence_loss_are_unknown(case, moved):
    session, manifest, _, _ = case
    record = capture(session, manifest)
    if moved:
        session._filesystem.rename(manifest, manifest.payload.actions[0])
    session.close()
    assert load(session, manifest) == record
    result = compare(session, manifest)
    present = result.target if moved else result.source
    assert present.content_match == present.filesystem_match == 'MATCH'
    assert present.physical_identity_match == result.outcome == 'UNKNOWN'


def test_inode_cannot_be_reused_while_original_pin_is_held(case):
    session, manifest, source, target = case
    record = capture(session, manifest)
    source.unlink()
    target.write_bytes(b'original scratch video')
    target.chmod(0o600)
    os.utime(target, ns=(record.filesystem.mtime_ns, record.filesystem.mtime_ns))
    result = compare(session, manifest)
    assert result.target is not None, result.diagnostics
    assert result.target.observed_file.st_ino != record.filesystem.st_ino
    assert result.target.content_match == 'MATCH' and result.outcome == 'MISMATCH'


def test_rename_with_only_pending_journal_is_diagnostic_not_resume(case):
    from app.target_execution_journal import DurableJournal, load_journal
    from app.target_execution_manifest import JournalEntry
    session, manifest, source, target = case
    capture(session, manifest)
    path = session.root.parent/'pending.jsonl'
    with DurableJournal.create(path, manifest) as writer:
        writer.append(JournalEntry(manifest.manifest_id, manifest.payload.actions[0].action_id, 0, 'PENDING'))
        session._filesystem.rename(manifest, manifest.payload.actions[0])
    session.close()
    raw = path.read_bytes()
    assert compare(session, manifest).outcome == 'UNKNOWN'
    assert [entry.state for entry in load_journal(path, manifest).entries] == ['PENDING']
    assert path.read_bytes() == raw and not source.exists() and target.exists()


@pytest.mark.parametrize('unsafe', ['source_symlink', 'target_symlink', 'hardlink', 'source_parent', 'target_parent', 'root'])
def test_unsafe_current_paths_never_yield_match(case, unsafe):
    session, manifest, source, target = case
    capture(session, manifest)
    if unsafe == 'source_symlink':
        original = source.with_name('original.mkv')
        source.rename(original)
        source.symlink_to(original)
    elif unsafe == 'target_symlink':
        target.symlink_to(source)
    elif unsafe == 'hardlink':
        os.link(source, target)
    elif unsafe == 'root':
        original = session.root.with_name(session.root.name + '-original')
        session.root.rename(original)
        session.root.symlink_to(original, target_is_directory=True)
    else:
        parent = source.parent if unsafe == 'source_parent' else target.parent
        original = parent.with_name(parent.name + '-original')
        parent.rename(original)
        parent.symlink_to(original, target_is_directory=True)
    assert compare(session, manifest).outcome == 'UNKNOWN'


def test_comparison_without_evidence_stops_without_creating_or_resuming(case):
    session, manifest, source, target = case
    assert compare(session, manifest).outcome == 'UNKNOWN'
    assert not session.evidence_path.exists() and source.exists() and not target.exists()
    with pytest.raises(api().IdentityEvidenceRejected):
        capture(session, manifest)


def test_load_does_not_accept_a_valid_prefix_from_short_read(case, monkeypatch):
    session, manifest, _, _ = case
    capture(session, manifest)
    raw = session.evidence_path.read_bytes()
    session.evidence_path.write_bytes(raw + b'unread trailing data')
    pread = os.pread
    def short_read(fd, count, offset):
        if os.readlink(f'/proc/self/fd/{fd}') == str(session.evidence_path):
            return raw
        return pread(fd, count, offset)
    monkeypatch.setattr(os, 'pread', short_read)
    with pytest.raises(api().IdentityEvidenceRejected):
        load(session, manifest)


@pytest.mark.parametrize('change', ['source_locator', 'target_appeared', 'evidence_bytes'])
def test_capture_rechecks_real_locators_and_artifact_after_final_hash(case, monkeypatch, change):
    session, manifest, source, target = case
    identity = api()
    hashing, calls = identity._hash_file, []
    before = source.stat()
    def mutate_after_read(fd):
        result = hashing(fd)
        calls.append(1)
        if len(calls) == 2:
            if change == 'source_locator':
                source.rename(source.with_name('held-original.mkv'))
                source.write_bytes(b'original scratch video')
                source.chmod(0o600)
                os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
            elif change == 'target_appeared':
                target.write_bytes(b'preserve concurrent target')
                target.chmod(0o600)
            else:
                session.evidence_path.write_bytes(b'preserve altered evidence')
        return result
    monkeypatch.setattr(identity, '_hash_file', mutate_after_read)
    with pytest.raises(identity.IdentityEvidenceReconciliationRequired):
        capture(session, manifest)
    assert not session._durable and session._poisoned
    raw = session.evidence_path.read_bytes()
    with pytest.raises(identity.IdentityEvidenceRejected):
        capture(session, manifest)
    assert session.evidence_path.read_bytes() == raw and calls == [1, 1]


@pytest.mark.parametrize('phase', ['before_open', 'partial_write', 'full_write', 'file_fsync', 'parent_fsync', 'post_hash'])
@pytest.mark.parametrize('fault_kind', ['io', 'keyboard', 'exit'])
def test_capture_faults_preserve_bytes_stop_once_and_never_execute(case, monkeypatch, phase, fault_kind):
    from app import target_execution_journal as journal
    session, manifest, source, target = case
    identity = api()
    fault = OSError(errno.EIO, 'injected evidence IO') if fault_kind == 'io' else KeyboardInterrupt('injected evidence interrupt') if fault_kind == 'keyboard' else SystemExit(123)
    write, sync, open_parent, hashing = identity._write_record, os.fsync, journal._open_parent, identity._hash_file
    writes, barriers, hashes = [], [], []
    def fail_write(fd, raw):
        writes.append(1)
        if phase == 'partial_write':
            os.write(fd, raw[:31])
            raise fault
        write(fd, raw)
        if phase == 'full_write':
            raise fault
    def fail_sync(fd):
        name = os.readlink(f'/proc/self/fd/{fd}')
        barriers.append(name)
        if phase == 'file_fsync' and name == str(session.evidence_path) or phase == 'parent_fsync' and name == str(session.root.parent):
            raise fault
        sync(fd)
    def fail_open(*args):
        if phase == 'before_open':
            raise fault
        return open_parent(*args)
    def fail_hash(fd):
        hashes.append(1)
        if phase == 'post_hash' and len(hashes) == 2:
            raise fault
        return hashing(fd)
    monkeypatch.setattr(identity, '_write_record', fail_write)
    monkeypatch.setattr(identity, '_hash_file', fail_hash)
    monkeypatch.setattr(os, 'fsync', fail_sync)
    monkeypatch.setattr(journal, '_open_parent', fail_open)
    monkeypatch.setattr(fs, '_rename_noreplace', lambda *args: pytest.fail('failed evidence allowed rename'))
    monkeypatch.setattr(journal.DurableJournal, 'create', lambda *args, **kwargs: pytest.fail('E3A journal creation'))
    expected = identity.IdentityEvidenceRejected if fault_kind == 'io' else type(fault)
    with pytest.raises(expected) as error:
        capture(session, manifest)
    if fault_kind != 'io':
        assert error.value is fault
        if fault_kind == 'exit':
            assert error.value.code == 123
    assert session._poisoned and not session._durable
    assert source.read_bytes() == b'original scratch video' and not target.exists()
    raw = session.evidence_path.read_bytes() if session.evidence_path.exists() else None
    if phase == 'before_open':
        assert raw is None and writes == []
    else:
        assert raw and writes == [1]
    counts = len(writes), len(barriers), len(hashes)
    with pytest.raises(identity.IdentityEvidenceRejected):
        capture(session, manifest)
    assert counts == (len(writes), len(barriers), len(hashes))
    assert raw == (session.evidence_path.read_bytes() if session.evidence_path.exists() else None)
    monkeypatch.setattr(journal, '_open_parent', open_parent)
    if phase not in {'before_open', 'partial_write'}:
        assert load(session, manifest).manifest_id == manifest.manifest_id
    assert compare(session, manifest).outcome == 'UNKNOWN'


@pytest.mark.parametrize('when', ['hashing', 'after_file_fsync', 'after_parent_fsync'])
def test_source_mutation_even_with_restored_size_mtime_stops_capture(case, monkeypatch, when):
    session, manifest, source, target = case
    identity = api()
    before = source.stat()
    pread, sync, changed = os.pread, os.fsync, []
    def mutate():
        source.write_bytes(b'Y' * before.st_size)
        os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
        changed.append(1)
    def changing_read(fd, size, offset):
        result = pread(fd, size, offset)
        if when == 'hashing' and not changed and os.readlink(f'/proc/self/fd/{fd}') == str(source):
            mutate()
        return result
    def changing_sync(fd):
        sync(fd)
        name = os.readlink(f'/proc/self/fd/{fd}')
        if not changed and (when == 'after_file_fsync' and name == str(session.evidence_path)
                or when == 'after_parent_fsync' and name == str(session.root.parent)):
            mutate()
    monkeypatch.setattr(os, 'pread', changing_read)
    monkeypatch.setattr(os, 'fsync', changing_sync)
    with pytest.raises(identity.IdentityEvidenceReconciliationRequired):
        capture(session, manifest)
    assert source.stat().st_size == before.st_size and source.stat().st_mtime_ns == before.st_mtime_ns
    assert changed == [1] and session._poisoned and not session._durable and not target.exists()


@pytest.mark.parametrize('unsafe', ['symlink', 'hardlink', 'parent_symlink'])
def test_load_rejects_unsafe_evidence_aliases(case, unsafe):
    session, manifest, _, _ = case
    capture(session, manifest)
    path = session.evidence_path
    saved = path.with_name('saved.identity.json')
    path.rename(saved)
    if unsafe == 'symlink':
        path.symlink_to(saved)
    elif unsafe == 'hardlink':
        os.link(saved, path)
    else:
        parent = path.parent
        original = parent.with_name(parent.name + '-original')
        parent.rename(original)
        parent.symlink_to(original, target_is_directory=True)
    with pytest.raises(api().IdentityEvidenceRejected):
        load(session, manifest)


def test_evidence_cannot_be_swapped_between_two_manifests(tmp_path):
    with api().ScratchIdentitySession.create(tmp_path) as first, api().ScratchIdentitySession.create(tmp_path) as second:
        first_manifest, _, _ = prepare(first)
        second_manifest, _, _ = prepare(second)
        capture(first, first_manifest)
        capture(second, second_manifest)
        second.evidence_path.write_bytes(first.evidence_path.read_bytes())
        with pytest.raises(api().IdentityEvidenceRejected):
            load(second, second_manifest)


@pytest.mark.parametrize('size', [0, 131075])
def test_content_hash_streams_complete_file_and_detects_eof(case, monkeypatch, size):
    session, manifest, source, _ = case
    expected = hashlib.sha256()
    with source.open('wb') as stream:
        remaining = size
        while remaining:
            chunk = b'Z' * min(remaining, 4096)
            stream.write(chunk)
            expected.update(chunk)
            remaining -= len(chunk)
    info = source.stat()
    manifest = seal(manifest, (changed_action(manifest, expected_size=size, expected_mtime_ns=info.st_mtime_ns),))
    pread, requests = os.pread, []
    def bounded_read(fd, count, offset):
        result = pread(fd, count, offset)
        if os.readlink(f'/proc/self/fd/{fd}') == str(source):
            requests.append((count, offset))
            assert 0 < count <= 65536
        return result
    monkeypatch.setattr(os, 'pread', bounded_read)
    record = capture(session, manifest)
    assert record.content.digest == expected.hexdigest() and record.content.bytes_hashed == size
    assert requests and requests[0][1] == 0
    if size:
        assert any(offset > 0 for _, offset in requests)


def test_large_scratch_file_is_rejected_before_content_read(case, monkeypatch):
    session, manifest, source, target = case
    with source.open('r+b') as stream:
        stream.truncate(8 * 1024 * 1024 + 1)
    info = source.stat()
    manifest = seal(manifest, (changed_action(manifest, expected_size=info.st_size, expected_mtime_ns=info.st_mtime_ns),))
    monkeypatch.setattr(os, 'pread', lambda *args: pytest.fail('oversize scratch file read'))
    with pytest.raises(api().IdentityEvidenceRejected):
        capture(session, manifest)
    assert not session.evidence_path.exists() and not target.exists()


def test_e3a_has_no_db_or_journal_write_lifecycle(case, monkeypatch):
    import sqlite3
    from app import target_execution_journal as journal
    session, manifest, source, target = case
    database = session.root.parent/'isolated.db'
    with sqlite3.connect(database) as conn:
        conn.execute('CREATE TABLE sentinel (value TEXT)')
        conn.execute("INSERT INTO sentinel VALUES ('preserve')")
    raw = database.read_bytes()
    monkeypatch.setattr(sqlite3, 'connect', lambda *args, **kwargs: pytest.fail('E3A database connection'))
    monkeypatch.setattr(journal.DurableJournal, 'create', lambda *args, **kwargs: pytest.fail('E3A journal creation'))
    monkeypatch.setattr(journal.DurableJournal, 'append', lambda *args, **kwargs: pytest.fail('E3A journal append'))
    monkeypatch.setattr(fs, '_rename_noreplace', lambda *args: pytest.fail('E3A rename'))
    capture(session, manifest)
    assert compare(session, manifest).outcome == 'MATCH'
    assert database.read_bytes() == raw and source.exists() and not target.exists()


@pytest.mark.parametrize('primary_kind,cleanup_kind', [
    ('stop', 'io'), ('stop', 'keyboard'), ('stop', 'exit'),
    ('keyboard', 'io'), ('exit', 'io'), ('keyboard', 'exit'), ('exit', 'exit'),
    (None, 'keyboard'), (None, 'exit'),
])
def test_identity_session_cleanup_preserves_exception_precedence_and_closes_once(tmp_path, monkeypatch, primary_kind, cleanup_kind):
    identity = api()
    session = identity.ScratchIdentitySession.create(tmp_path)
    manifest, _, _ = prepare(session)
    capture(session, manifest)
    primary = None if primary_kind is None else identity.IdentityEvidenceRejected('primary STOP') if primary_kind == 'stop' else KeyboardInterrupt('primary interrupt') if primary_kind == 'keyboard' else SystemExit(71)
    cleanup = OSError(errno.EIO, 'cleanup IO') if cleanup_kind == 'io' else KeyboardInterrupt('cleanup interrupt') if cleanup_kind == 'keyboard' else SystemExit(99)
    fds = (session._pin_fd, session._source_parent_fd, session._target_parent_fd, session._filesystem._root_fd)
    close, calls = os.close, []
    def fail_close(fd):
        close(fd)
        if fd in fds:
            calls.append(fd)
        if fd == session._pin_fd:
            raise cleanup
    monkeypatch.setattr(os, 'close', fail_close)
    expected = primary if isinstance(primary, (KeyboardInterrupt, SystemExit)) or primary is not None and isinstance(cleanup, Exception) else cleanup
    with pytest.raises(BaseException) as error:
        with session:
            if primary is not None:
                raise primary
    assert error.value is expected and expected.__traceback__ is not None
    if isinstance(expected, SystemExit):
        assert expected.code == (71 if expected is primary else 99)
    if primary is not None and expected is cleanup:
        assert cleanup.__cause__ is primary
    elif primary is not None:
        assert any(type(cleanup).__name__ in note for note in primary.__notes__)
    assert sorted(calls) == sorted(fds) and session._closed
    session.close()
    assert sorted(calls) == sorted(fds)
    with pytest.raises(identity.IdentityEvidenceRejected):
        capture(session, manifest)


@pytest.mark.parametrize('signal_type', [KeyboardInterrupt, SystemExit])
def test_partial_write_stop_cleanup_interruption_has_priority(case, monkeypatch, signal_type):
    session, manifest, source, target = case
    identity = api()
    primary, signal = OSError(errno.EIO, 'partial evidence STOP'), signal_type(127)
    close, artifact_closes = os.close, []
    def fail_write(fd, raw):
        os.write(fd, raw[:19])
        raise primary
    def interrupt_close(fd):
        name = os.readlink(f'/proc/self/fd/{fd}')
        close(fd)
        if name == str(session.evidence_path):
            artifact_closes.append(1)
            raise signal
    monkeypatch.setattr(identity, '_write_record', fail_write)
    monkeypatch.setattr(os, 'close', interrupt_close)
    with pytest.raises(signal_type) as error:
        capture(session, manifest)
    assert error.value is signal and signal.__cause__ is primary
    if isinstance(signal, SystemExit):
        assert signal.code == 127
    assert artifact_closes == [1] and session._poisoned and not session._durable
    assert session.evidence_path.read_bytes() and source.exists() and not target.exists()


def test_comparison_io_or_mutation_yields_unknown_without_retry(case, monkeypatch):
    session, manifest, source, target = case
    capture(session, manifest)
    hashing, reads = api()._hash_file, []
    def fail_read(fd):
        reads.append(1)
        raise OSError(errno.EIO, 'read-only IO')
    monkeypatch.setattr(api(), '_hash_file', fail_read)
    assert compare(session, manifest).outcome == 'UNKNOWN'
    assert reads == [1] and source.exists() and not target.exists()
    assert compare(session, manifest).outcome == 'UNKNOWN' and reads == [1]
    monkeypatch.setattr(api(), '_hash_file', hashing)


@pytest.mark.parametrize('signal_type', [KeyboardInterrupt, SystemExit])
def test_comparison_interrupt_preserves_signal_and_stop(case, monkeypatch, signal_type):
    session, manifest, source, target = case
    capture(session, manifest)
    signal = signal_type(129)
    def interrupt_read(fd):
        raise signal
    monkeypatch.setattr(api(), '_hash_file', interrupt_read)
    with pytest.raises(signal_type) as error:
        compare(session, manifest)
    assert error.value is signal and session._poisoned
    if isinstance(signal, SystemExit):
        assert signal.code == 129
    assert source.exists() and not target.exists()


@pytest.mark.parametrize('change', ['source_parent', 'target_parent', 'mount_table', 'file_mount'])
def test_mount_or_parent_replacement_is_unknown(case, monkeypatch, change):
    session, manifest, source, target = case
    capture(session, manifest)
    if change in {'source_parent', 'target_parent'}:
        parent = source.parent if change == 'source_parent' else target.parent
        parent.rename(parent.with_name(parent.name + '-original'))
        parent.mkdir(mode=0o700)
    elif change == 'mount_table':
        mounts = fs._read_mounts()
        monkeypatch.setattr(fs, '_read_mounts', lambda: tuple(replace(m, raw=m.raw + ' injected') for m in mounts))
    else:
        mount_id = fs._mount_id
        def crossed(fd):
            return mount_id(fd) + 1 if os.readlink(f'/proc/self/fd/{fd}') == str(source) else mount_id(fd)
        monkeypatch.setattr(fs, '_mount_id', crossed)
    assert compare(session, manifest).outcome == 'UNKNOWN'


@pytest.mark.parametrize('invalid', ['traversal', 'parent_symlink', 'source_symlink', 'source_hardlink', 'source_missing', 'target_parent_missing'])
def test_capture_rejects_unsafe_scratch_sources_without_artifact(case, invalid):
    session, manifest, source, target = case
    if invalid == 'traversal':
        manifest = seal(manifest, (changed_action(manifest, source='../outside.mkv'),))
    elif invalid == 'parent_symlink':
        original = source.parent.with_name('original-parent')
        source.parent.rename(original)
        source.parent.symlink_to(original, target_is_directory=True)
    elif invalid == 'source_symlink':
        original = source.with_name('original.mkv')
        source.rename(original)
        source.symlink_to(original)
    elif invalid == 'source_hardlink':
        os.link(source, source.with_name('second-link.mkv'))
    elif invalid == 'source_missing':
        source.unlink()
    else:
        target.parent.rmdir()
    with pytest.raises(api().IdentityEvidenceRejected):
        capture(session, manifest)
    assert not session.evidence_path.exists() and not target.exists()


def test_standalone_identity_does_not_invoke_coordination(tmp_path, monkeypatch):
    from app.target_execution_coordination import ScratchActionSession
    monkeypatch.setattr(ScratchActionSession, 'execute', lambda *args, **kwargs: pytest.fail('standalone E3A executed'))
    with api().ScratchIdentitySession.create(tmp_path) as session:
        manifest, source, target = prepare(session)
        record = capture(session, manifest)
        assert session.compare(manifest, record.action, action_index=0).outcome == 'MATCH'
        assert source.exists() and not target.exists()
        assert not list(tmp_path.glob('*.journal.jsonl'))


def crash_probe(parent, phase, sender):
    """Child allocates its own authorised context; inherited sessions are forbidden."""
    identity = api()
    session = identity.ScratchIdentitySession.create(Path(parent))
    manifest, _, _ = prepare(session)
    sender.send((str(session.evidence_path), manifest_to_json(manifest)))
    sender.close()
    if phase == 'before_evidence':
        os._exit(62)
    if phase in {'partial_write', 'full_write'}:
        write = identity._write_record
        def crash_write(fd, raw):
            if phase == 'partial_write':
                os.write(fd, raw[:37])
            else:
                write(fd, raw)
            os._exit(62)
        identity._write_record = crash_write
    capture(session, manifest)
    if phase == 'renamed_pending':
        from app.target_execution_journal import DurableJournal
        from app.target_execution_manifest import JournalEntry
        writer = DurableJournal.create(session.root.parent/'crash-pending.jsonl', manifest)
        writer.append(JournalEntry(manifest.manifest_id, manifest.payload.actions[0].action_id, 0, 'PENDING'))
        session._filesystem.rename(manifest, manifest.payload.actions[0])
    os._exit(62)


@pytest.mark.parametrize('phase', ['before_evidence', 'partial_write', 'full_write', 'durable_before_rename', 'renamed_pending'])
def test_process_loss_keeps_diagnostic_bytes_and_has_no_restart_api(tmp_path, phase):
    import multiprocessing
    from app.target_execution_manifest import manifest_from_json
    from app.target_execution_journal import load_journal
    ctx = multiprocessing.get_context('fork')
    receiver, sender = ctx.Pipe(duplex=False)
    process = ctx.Process(target=crash_probe, args=(str(tmp_path), phase, sender))
    process.start()
    sender.close()
    try:
        assert receiver.poll(15), 'child did not publish scratch binding'
        evidence_path, raw_manifest = receiver.recv()
        process.join(15)
        assert not process.is_alive() and process.exitcode == 62
        manifest = manifest_from_json(raw_manifest)
        root = Path(manifest.payload.library_root)
        assert root.is_relative_to(tmp_path) and Path(evidence_path).parent == tmp_path
        with pytest.raises(TypeError):
            api().ScratchIdentitySession.create(tmp_path, root=root)
        path = Path(evidence_path)
        if phase == 'before_evidence':
            assert not path.exists()
        elif phase == 'partial_write':
            with pytest.raises(api().IdentityEvidenceRejected):
                api()._decode_frame(path.read_bytes())
        else:
            parsed = api()._decode_frame(path.read_bytes())
            assert parsed.manifest_id == manifest.manifest_id and parsed.action == manifest.payload.actions[0]
        assert (root/'old/source.mkv').exists() is (phase != 'renamed_pending')
        assert (root/'new/target.mkv').exists() is (phase == 'renamed_pending')
        if phase == 'renamed_pending':
            snapshot = load_journal(tmp_path/'crash-pending.jsonl', manifest)
            assert [entry.state for entry in snapshot.entries] == ['PENDING']
            assert snapshot.reconciliation_required is True
    finally:
        receiver.close()
        if process.is_alive():
            process.terminate()
            process.join(5)


def test_nonzero_action_index_binds_the_exact_selected_action(case):
    session, manifest, _, _ = case
    source = session.root/'old/z.mkv'
    source.write_bytes(b'second isolated asset')
    source.chmod(0o600)
    info = source.stat()
    second = changed_action(manifest, object_id=2, source='old/z.mkv', target='new/z.mkv',
        expected_size=info.st_size, expected_mtime_ns=info.st_mtime_ns)
    manifest = seal(manifest, (manifest.payload.actions[0], second))
    record = session.capture(manifest, second, action_index=1)
    assert record.action_index == 1 and record.action.object_id == 2
    assert session.load(manifest, second, action_index=1) == record
    assert session.compare(manifest, second, action_index=1).outcome == 'MATCH'
    with pytest.raises(api().IdentityEvidenceRejected):
        session.load(manifest, second, action_index=0)


@pytest.mark.parametrize('error_number', [errno.EACCES, errno.EIO, errno.EINTR])
def test_failed_evidence_write_is_not_retried(case, monkeypatch, error_number):
    session, manifest, source, target = case
    write, calls = os.write, []
    def interrupted_write(fd, raw):
        if os.readlink(f'/proc/self/fd/{fd}') == str(session.evidence_path):
            calls.append(1)
            raise OSError(error_number, 'injected write failure')
        return write(fd, raw)
    monkeypatch.setattr(os, 'write', interrupted_write)
    with pytest.raises(api().IdentityEvidenceReconciliationRequired):
        capture(session, manifest)
    assert calls == [1] and session.evidence_path.read_bytes() == b''
    with pytest.raises(api().IdentityEvidenceRejected):
        capture(session, manifest)
    assert calls == [1] and source.exists() and not target.exists()


def test_short_writes_are_completed_before_acknowledgement(case, monkeypatch):
    session, manifest, _, _ = case
    write = os.write
    def short_write(fd, raw):
        return write(fd, raw[:29])
    monkeypatch.setattr(os, 'write', short_write)
    record = capture(session, manifest)
    assert load(session, manifest) == record and compare(session, manifest).outcome == 'MATCH'


def test_inherited_context_rejects_before_waiting_on_inherited_mutex(case, monkeypatch):
    session, manifest, _, _ = case
    capture(session, manifest)
    pid = os.getpid()
    session._mutex.acquire()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(os, 'getpid', lambda: pid + 1)
            for operation in (capture, load, compare):
                with pytest.raises(api().IdentityEvidenceRejected):
                    operation(session, manifest)
    finally:
        session._mutex.release()


@pytest.mark.parametrize('change', ['source_mode', 'source_hardlink', 'artifact_mount'])
def test_rejection_after_durable_artifact_write_requires_reconciliation(case, monkeypatch, change):
    session, manifest, source, target = case
    sync, read_mounts, changed = os.fsync, fs._read_mounts, []
    def mutate_after_parent_barrier(fd):
        sync(fd)
        if os.readlink(f'/proc/self/fd/{fd}') == str(session.root.parent) and not changed:
            changed.append(1)
            if change == 'source_mode':
                source.chmod(0o620)
            elif change == 'source_hardlink':
                os.link(source, source.with_name('alias.mkv'))
            else:
                mounts = read_mounts()
                monkeypatch.setattr(fs, '_read_mounts', lambda: mounts[:-1])
    monkeypatch.setattr(os, 'fsync', mutate_after_parent_barrier)
    # A complete, fsynced record exists, so a plain rejection must not hide uncertainty.
    with pytest.raises(api().IdentityEvidenceReconciliationRequired):
        capture(session, manifest)
    assert changed == [1] and session.evidence_path.read_bytes()
    assert session._poisoned and not session._durable and not target.exists()


@pytest.mark.parametrize('tamper', ['rechecksummed_digest', 'same_bytes_new_inode', 'ctime_only'])
def test_live_compare_refuses_changed_acknowledged_artifact(case, tamper):
    session, manifest, source, _ = case
    capture(session, manifest)
    path = session.evidence_path
    if tamper == 'rechecksummed_digest':
        # Same pinned inode, changed bytes, and a strict record claiming them.
        info = source.stat()
        source.write_bytes(b'X' * info.st_size)
        os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns))
        frame = json.loads(path.read_bytes())
        frame['evidence']['content']['digest'] = hashlib.sha256(source.read_bytes()).hexdigest()
        frame['sha256'] = hashlib.sha256(canonical(frame['evidence']).encode()).hexdigest()
        path.write_bytes((canonical(frame) + '\n').encode())
    elif tamper == 'same_bytes_new_inode':
        raw = path.read_bytes()
        path.rename(path.with_name('displaced.identity.json'))
        path.write_bytes(raw)
        path.chmod(0o600)
    else:
        info = path.stat()
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
    # Comparison first: only the acknowledged in-memory capture anchor can refuse it.
    assert compare(session, manifest).outcome == 'UNKNOWN'
    with pytest.raises(api().IdentityEvidenceReconciliationRequired):
        load(session, manifest)
