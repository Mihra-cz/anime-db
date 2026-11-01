"""Versioned E3 metadata evidence uses private temporary files only."""
from contextlib import contextmanager
from dataclasses import asdict, replace
import hashlib
import json
import os
import threading
from types import SimpleNamespace

import pytest

from app import target_execution_filesystem as fs
from app import target_execution_identity as identity
from test_v6_execution_filesystem import prepare


@contextmanager
def metadata_case(tmp_path):
    with fs.ScratchRenameSession.create(tmp_path) as filesystem:
        manifest, source, target = prepare(filesystem)
        mutex = threading.Lock()
        with mutex:
            owner = SimpleNamespace(_filesystem=filesystem, _closed=False,
                _manifest=manifest, _mutex=mutex, _pid=os.getpid(),
                _manifest_sha256=hashlib.sha256(identity.manifest_to_json(manifest).encode()).hexdigest(),
                database_path=filesystem.root.parent / (filesystem.root.name + '.sqlite'),
                _metadata_rename=lambda index: index == 0,
                _check_storage_fd=lambda fd: None)
            with identity.ScratchIdentitySession._new_execution_context(filesystem, owner, 0) as session:
                yield session, manifest, source, target


def forbid_payload_reads(monkeypatch, source):
    """Permit evidence and mount reads, reject actual media descriptor reads."""
    before = source.stat()
    key = before.st_dev, before.st_ino
    for name in ('read', 'pread'):
        original = getattr(os, name)

        def checked(fd, *args, original=original):
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) == key:
                pytest.fail('METADATA_RENAME read media payload')
            return original(fd, *args)

        monkeypatch.setattr(os, name, checked)
    monkeypatch.setattr(identity, '_hash_file',
        lambda *args: pytest.fail('METADATA_RENAME called the FULL content hasher'))


def capture(session, manifest):
    return session.capture(manifest, manifest.payload.actions[0], action_index=0)


def compare(session, manifest):
    return session.compare(manifest, manifest.payload.actions[0], action_index=0)


def frame(body):
    canonical = lambda value: json.dumps(value, sort_keys=True, separators=(',', ':'),
        ensure_ascii=True, allow_nan=False)
    digest = hashlib.sha256(canonical(body).encode()).hexdigest()
    return (canonical({'evidence': body, 'sha256': digest}) + '\n').encode()


@pytest.mark.parametrize('moved', [False, True])
def test_metadata_capture_and_live_comparison_never_read_media_payload(tmp_path, monkeypatch, moved):
    with metadata_case(tmp_path) as (session, manifest, source, target):
        before = source.stat()
        forbid_payload_reads(monkeypatch, source)
        record = capture(session, manifest)
        assert (record.version, record.mode, record.content) == (2, 'METADATA_RENAME', None)
        assert record.filesystem.st_ino == before.st_ino
        assert record.filesystem.ctime_ns == before.st_ctime_ns
        body = json.loads(session.evidence_path.read_bytes())['evidence']
        assert body['mode'] == 'METADATA_RENAME' and body['content'] is None
        assert session.load(manifest, record.action, action_index=0) == record
        pins = session._pin_fd, session._source_parent_fd, session._target_parent_fd
        assert all(os.fstat(fd).st_ino > 0 for fd in pins)
        if moved:
            session._filesystem.rename(manifest, record.action)
        result = compare(session, manifest)
        assert result.outcome == 'METADATA_MATCH', result.diagnostics
        present, absent = (result.target, result.source) if moved else (result.source, result.target)
        assert present.observed_content is None and present.content_match == 'NOT_CHECKED'
        assert present.filesystem_match == present.physical_identity_match == 'MATCH'
        assert absent.presence == 'ABSENT'
        assert source.exists() is not moved and target.exists() is moved
        assert all(os.fstat(fd).st_ino > 0 for fd in pins)


@pytest.mark.parametrize('barrier', ['file', 'parent'])
def test_metadata_capture_rejects_same_size_and_mtime_mutation_during_persistence(tmp_path, monkeypatch, barrier):
    with metadata_case(tmp_path) as (session, manifest, source, target):
        before, sync = source.stat(), os.fsync
        changed = []
        forbid_payload_reads(monkeypatch, source)

        def mutate(fd):
            sync(fd)
            selected = session.evidence_path if barrier == 'file' else session.evidence_path.parent
            if not changed and os.readlink(f'/proc/self/fd/{fd}') == str(selected):
                source.write_bytes(b'X' * before.st_size)
                os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
                changed.append(True)

        monkeypatch.setattr(os, 'fsync', mutate)
        with pytest.raises(identity.IdentityEvidenceReconciliationRequired):
            capture(session, manifest)
        assert changed and not session._durable and session._poisoned
        assert source.stat().st_size == before.st_size
        assert source.stat().st_mtime_ns == before.st_mtime_ns
        assert session.evidence_path.exists() and not target.exists()


@pytest.mark.parametrize('change,want', [('replacement', 'MISMATCH'),
    ('mtime', 'MISMATCH'), ('source_symlink', 'UNKNOWN'),
    ('target_symlink', 'UNKNOWN'), ('hardlink', 'UNKNOWN'), ('lost_pin', 'UNKNOWN')])
def test_metadata_comparison_preserves_identity_and_nofollow_stops(tmp_path, monkeypatch, change, want):
    with metadata_case(tmp_path) as (session, manifest, source, target):
        record = capture(session, manifest)
        forbid_payload_reads(monkeypatch, source)
        if change == 'replacement':
            source.unlink()
            target.write_bytes(b'original scratch video')
            target.chmod(0o600)
            os.utime(target, ns=(record.filesystem.mtime_ns, record.filesystem.mtime_ns))
        elif change == 'mtime':
            os.utime(source, ns=(record.filesystem.mtime_ns, record.filesystem.mtime_ns + 1))
        elif change == 'source_symlink':
            original = source.with_name('original.mkv')
            source.rename(original)
            source.symlink_to(original)
        elif change == 'target_symlink':
            target.symlink_to(source)
        elif change == 'hardlink':
            os.link(source, target)
        else:
            session.close()
        result = compare(session, manifest)
        assert result.outcome == want, result.diagnostics
        if change in {'replacement', 'mtime', 'lost_pin'}:
            present = result.target if change == 'replacement' else result.source
            assert present.content_match == 'NOT_CHECKED' and present.observed_content is None


@pytest.fixture
def legacy_record(tmp_path):
    with identity.ScratchIdentitySession.create(tmp_path) as session:
        manifest, source, _ = prepare(session)
        record = capture(session, manifest)
        yield session, manifest, source, record


def test_legacy_full_capture_preserves_exact_v1_frame_and_content_proof(legacy_record):
    session, manifest, _, record = legacy_record
    assert record.mode == 'FULL' and record.version == 1
    assert record.content == identity.ContentEvidence('sha256',
        '9b7080928611c26681c9b3ec3c9067065117368b09c737d72fda1219da79b90f', 'FULL', 22)
    old_body = asdict(record)
    old_body.pop('mode')
    old_raw = frame(old_body)
    assert session.evidence_path.read_bytes() == old_raw == identity._frame(record)
    assert identity._decode_frame(old_raw) == record
    assert compare(session, manifest).outcome == 'MATCH'


def test_metadata_frame_is_explicit_and_roundtrips_without_content_claim(legacy_record):
    _, _, _, full = legacy_record
    body = asdict(full)
    body.update(version=2, mode='METADATA_RENAME', content=None)
    raw = frame(body)
    record = identity._decode_frame(raw)
    assert record.version == 2 and record.mode == 'METADATA_RENAME' and record.content is None
    assert identity._frame(record) == raw
    identity._validate_content(record)


def test_serialization_cannot_hide_an_unsupported_mode_in_legacy_bytes(legacy_record):
    _, _, _, record = legacy_record
    with pytest.raises(identity.IdentityEvidenceRejected):
        identity._frame(replace(record, mode='UNSUPPORTED'))


@pytest.mark.parametrize('change', ['schema', 'version', 'missing_mode', 'unknown_mode',
    'v2_full', 'metadata_hash_claim', 'legacy_explicit_mode', 'legacy_no_content',
    'partial', 'short_hash', 'wrong_size', 'unsafe_file'])
def test_decoder_rejects_unsupported_modes_and_false_content_claims(legacy_record, change):
    _, _, _, full = legacy_record
    body = asdict(full)
    body.pop('mode', None)
    if change in {'schema', 'version', 'missing_mode', 'unknown_mode', 'v2_full',
            'metadata_hash_claim', 'unsafe_file'}:
        body.update(version=2, mode='METADATA_RENAME', content=None)
    if change == 'schema':
        body['schema'] = 'other-evidence'
    elif change == 'version':
        body['version'] = 99
    elif change == 'missing_mode':
        body.pop('mode')
    elif change == 'unknown_mode':
        body['mode'] = 'FAST'
    elif change == 'v2_full':
        body.update(mode='FULL', content=asdict(full.content))
    elif change == 'metadata_hash_claim':
        body['content'] = asdict(full.content)
    elif change == 'legacy_explicit_mode':
        body['mode'] = 'FULL'
    elif change == 'legacy_no_content':
        body['content'] = None
    elif change == 'partial':
        body['content']['coverage'] = 'PARTIAL'
    elif change == 'short_hash':
        body['content']['digest'] = 'abc'
    elif change == 'wrong_size':
        body['content']['bytes_hashed'] = 0
    else:
        body['filesystem']['nlink'] = 2
    with pytest.raises(identity.IdentityEvidenceRejected):
        identity._decode_frame(frame(body))


@pytest.mark.parametrize('change', ['version', 'mode', 'content_type', 'content_none', 'boolean_bytes'])
def test_content_validator_rejects_invalid_in_memory_evidence(legacy_record, change):
    _, _, _, record = legacy_record
    if change == 'version':
        record = replace(record, version=True)
    elif change == 'mode':
        record = replace(record, mode='METADATA_RENAME')
    elif change == 'content_type':
        record = replace(record, content=asdict(record.content))
    elif change == 'content_none':
        record = replace(record, content=None)
    else:
        record = replace(record, filesystem=replace(record.filesystem, size=1),
            content=replace(record.content, bytes_hashed=True))
    with pytest.raises(identity.IdentityEvidenceRejected):
        identity._validate_content(record)
