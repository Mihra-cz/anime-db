"""Approval/handoff uses real local fixtures; never reaches NAS or executes E7."""
from dataclasses import asdict, replace
import hashlib
from pathlib import Path

import pytest

from app.target_execution_manifest import manifest_to_json
from test_v6_production import production, SHARE


def digest(manifest):
    return hashlib.sha256(manifest_to_json(manifest).encode()).hexdigest()


def snapshot(database, root):
    return {str(p): (p.stat().st_size, p.stat().st_mtime_ns,
                    hashlib.sha256(p.read_bytes()).hexdigest())
            for p in [database, *root.rglob('*')] if p.is_file()}


@pytest.fixture
def case(production, tmp_path):
    owner, database, manifest, runtime = production
    from sqlalchemy.orm import Session
    from app.database import make_engine
    from app.models import UnresolvedExternalSubtitle
    from test_v6_execution_file_actions import generate
    orphan = owner.root / 'old/extra.cs.srt'
    orphan.write_bytes(b'confirmed subtitle without a video')
    orphan.chmod(0o600)
    engine = make_engine(f'sqlite:///{database}')
    try:
        with Session(engine) as session:
            session.add(UnresolvedExternalSubtitle(relative_path='old/extra.cs.srt',
                filename='extra.cs.srt', extension='.srt', status='confirmed_no_match'))
            session.commit()
    finally:
        engine.dispose()
    manifest = generate(owner, database)
    Path(runtime.db_backup_path).write_bytes(database.read_bytes())
    p = manifest.payload.db_fingerprint
    runtime = replace(runtime, snapshot_manifest_id=manifest.manifest_id,
        db_backup_manifest_id=manifest.manifest_id, db_backup_sha256=p.sha256,
        db_backup_size=p.size, db_backup_user_version=p.user_version)
    from app import reorganization_approval as approval
    inbox, outbox, ledger = (tmp_path / name for name in ('inbox', 'outbox', 'ledger'))
    for p in (inbox, outbox, ledger):
        p.mkdir(mode=0o700)
    lock = database.parent / 'runtime.lock'
    lock.write_bytes(b'')
    lock.chmod(0o600)
    return approval, owner, database, manifest, runtime, inbox, outbox, ledger, lock


def publish(case):
    api, owner, db, manifest, runtime, inbox, *_ = case
    return api.publish_proposal(inbox=inbox, database=db, library_root=owner.root,
                                share=SHARE, share_root='/', manifest=manifest, runtime=runtime)


def approve(case, source):
    api, owner, db, manifest, runtime, inbox, outbox, *_ = case
    bundle = api.read_proposal(inbox, source)
    final = api.prepare_review(bundle, actor='operator',
        warning_ids=[w.warning_id for w in manifest.payload.warnings])
    api.write_review(outbox, source, final)
    api.write_approval(outbox, source, final, actor='operator', approved_sha256=digest(final))
    return final


def accept(case, source, final, **overrides):
    api, owner, db, manifest, runtime, inbox, outbox, ledger, lock = case
    kwargs = dict(inbox=inbox, outbox=outbox, ledger=ledger, source_sha256=source,
        approved_sha256=digest(final), actor='operator', database=db, library_root=owner.root,
        share=SHARE, share_root='/', runtime=runtime, runtime_lock=lock, writers_stopped=True)
    kwargs.update(overrides)
    return api.accept_approval(**kwargs)


def test_canonical_approval_and_disabled_host_acceptance(case):
    api, owner, db, manifest, runtime, inbox, outbox, ledger, lock = case
    before = snapshot(db, owner.root)
    source = publish(case)
    assert source == digest(manifest)
    bundle = api.read_proposal(inbox, source)
    final = approve(case, source)
    result = api.check_proposal(bundle, final, database=db, library_root=owner.root)
    assert result.outcome == 'READY_FOR_EXECUTION'
    receipt = accept(case, source, final)
    assert receipt['state'] == 'ACCEPTED_EXECUTION_DISABLED'
    assert receipt['approved_sha256'] == digest(final)
    assert receipt['started'] is False
    assert snapshot(db, owner.root) == before
    with pytest.raises((ValueError, FileExistsError)):
        accept(case, source, final)


@pytest.mark.parametrize('fault', ['hash', 'payload', 'envelope', 'db', 'filesystem', 'runtime', 'mount'])
def test_handoff_rejects_without_effect(case, fault, monkeypatch):
    api, owner, db, manifest, runtime, inbox, outbox, ledger, lock = case
    source = publish(case)
    final = approve(case, source)
    if fault == 'db':
        import sqlite3
        with sqlite3.connect(db) as connection:
            connection.execute('PRAGMA user_version=8')
    elif fault == 'filesystem':
        (owner.root / manifest.payload.actions[0].source).write_bytes(b'changed')
    elif fault in {'payload', 'envelope'}:
        from app.target_execution_manifest import _digest
        altered_payload = replace(final.payload, mount_identity='cifs://forged:0:0')
        altered = (replace(final, payload=altered_payload, manifest_id=_digest(asdict(altered_payload)))
                   if fault == 'payload' else replace(final, created_at='2026-01-01T00:00:00Z'))
        (outbox / f'{source}.review.json').write_text(manifest_to_json(altered))
    elif fault == 'mount':
        from app import target_execution_mounts as mounts
        monkeypatch.setattr(mounts, '_check_production_mount', lambda *a, **k: (_ for _ in ()).throw(ValueError('wrong RW mount')))
    before = snapshot(db, owner.root)
    overrides = {'approved_sha256': '0' * 64} if fault == 'hash' else {}
    if fault == 'runtime':
        overrides['runtime'] = replace(runtime, mount_read_write_checked=False)
    with pytest.raises((ValueError, RuntimeError)):
        accept(case, source, final, **overrides)
    assert snapshot(db, owner.root) == before
    assert not list(ledger.iterdir())


def test_warning_confirmation_and_create_once_receipt(case):
    api, owner, db, manifest, runtime, inbox, outbox, *_ = case
    source = publish(case)
    bundle = api.read_proposal(inbox, source)
    assert manifest.payload.warnings
    with pytest.raises(ValueError, match='warning'):
        api.prepare_review(bundle, actor='operator', warning_ids=[])
    final = approve(case, source)
    with pytest.raises(FileExistsError):
        api.write_approval(outbox, source, final, actor='operator', approved_sha256=digest(final))
    with pytest.raises(ValueError):
        api.write_approval(outbox, source, final, actor='operator', approved_sha256='0' * 64)


def test_runtime_lock_and_interruption_before_acceptance(case, monkeypatch):
    import fcntl
    api, owner, db, manifest, runtime, inbox, outbox, ledger, lock = case
    source = publish(case)
    final = approve(case, source)
    before = snapshot(db, owner.root)
    with lock.open('r+') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(Exception, match='runtime.lock'):
            accept(case, source, final)
    monkeypatch.setattr(api, 'check_proposal', lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        accept(case, source, final)
    assert snapshot(db, owner.root) == before
    assert not list(ledger.iterdir())


def test_unresolved_review_cannot_be_published(case):
    from sqlalchemy.orm import Session
    from app.database import make_engine
    from app.models import Video
    from test_v6_execution_file_actions import generate
    api, owner, db, manifest, runtime, inbox, *_ = case
    engine = make_engine(f'sqlite:///{db}')
    try:
        with Session(engine) as session:
            session.get(Video, 100).duplicate_status_manual = 'suspected'
            session.commit()
    finally:
        engine.dispose()
    invalid = generate(owner, db)
    assert invalid.payload.generation_diagnostics
    before = snapshot(db, owner.root)
    with pytest.raises(ValueError, match='preflight'):
        api.publish_proposal(inbox=inbox, database=db, library_root=owner.root,
            share=SHARE, share_root='/', manifest=invalid, runtime=runtime)
    assert not list(inbox.iterdir())
    assert snapshot(db, owner.root) == before


def test_expired_host_evidence_is_stop(case):
    api, owner, db, manifest, runtime, inbox, *_ = case
    source = publish(case)
    bundle = api.read_proposal(inbox, source)
    bundle['published_at'] = '2020-01-01T00:00:00+00:00'
    final = api.prepare_review(bundle, actor='operator', warning_ids=[w.warning_id for w in manifest.payload.warnings])
    before = snapshot(db, owner.root)
    with pytest.raises(ValueError, match='expired'):
        api.check_proposal(bundle, final, database=db, library_root=owner.root)
    assert snapshot(db, owner.root) == before


def test_missing_lock_and_stopped_writer_confirmation_are_stop(case):
    api, owner, db, manifest, runtime, inbox, outbox, ledger, lock = case
    source = publish(case)
    final = approve(case, source)
    before = snapshot(db, owner.root)
    with pytest.raises(ValueError, match='writers'):
        accept(case, source, final, writers_stopped=False)
    with pytest.raises(FileNotFoundError):
        accept(case, source, final, runtime_lock=lock.with_name('missing.lock'))
    assert snapshot(db, owner.root) == before
    assert not list(ledger.iterdir())


def test_uncertain_partial_host_receipt_is_spent_without_retry(case, monkeypatch):
    api, owner, db, manifest, runtime, inbox, outbox, ledger, lock = case
    source = publish(case)
    final = approve(case, source)
    before = snapshot(db, owner.root)
    from app import target_execution_journal as journal
    import os
    def interrupted(fd, raw):
        os.write(fd, raw[:10])
        raise KeyboardInterrupt()
    monkeypatch.setattr(journal, '_write_all', interrupted)
    with pytest.raises(KeyboardInterrupt):
        accept(case, source, final)
    assert list(ledger.iterdir())
    with pytest.raises(FileExistsError):
        accept(case, source, final)
    assert not (inbox / f'{source}.acceptance.json').exists()
    assert snapshot(db, owner.root) == before


def test_real_local_directory_is_not_an_authorized_rw_cifs_mount(tmp_path):
    from app import reorganization_approval as api
    from app.target_execution_manifest import RuntimeEvidence
    with pytest.raises(ValueError, match='server/share/subtree'):
        api.worker_runtime(tmp_path, share=SHARE, share_root='/', runtime=RuntimeEvidence())


def test_same_payload_cannot_be_accepted_via_new_envelope(case):
    api, owner, db, manifest, runtime, inbox, outbox, ledger, lock = case
    source = publish(case)
    final = approve(case, source)
    accept(case, source, final)
    new_original = replace(manifest, created_at='2026-10-19T12:00:01Z')
    newer = replace(runtime, snapshot_at='2026-10-19T12:00:02Z')
    new_case = (api, owner, db, new_original, newer, inbox, outbox, ledger, lock)
    next_source = publish(new_case)
    next_final = approve(new_case, next_source)
    assert next_source != source and next_final.manifest_id == final.manifest_id
    before = snapshot(db, owner.root)
    with pytest.raises(FileExistsError):
        accept(new_case, next_source, next_final)
    assert snapshot(db, owner.root) == before


def test_concurrent_approval_receipts_are_create_once(case):
    from concurrent.futures import ThreadPoolExecutor
    api, owner, db, manifest, runtime, inbox, outbox, *_ = case
    source = publish(case)
    bundle = api.read_proposal(inbox, source)
    final = api.prepare_review(bundle, actor='operator', warning_ids=[w.warning_id for w in manifest.payload.warnings])
    api.write_review(outbox, source, final)
    def submit():
        try:
            api.write_approval(outbox, source, final, actor='operator', approved_sha256=digest(final))
            return 'created'
        except FileExistsError:
            return 'exists'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: submit(), range(2)))
    assert sorted(results) == ['created', 'exists']


def test_symlinked_review_and_altered_original_are_stop(case):
    api, owner, db, manifest, runtime, inbox, outbox, ledger, lock = case
    source = publish(case)
    final = approve(case, source)
    before = snapshot(db, owner.root)
    review = outbox / f'{source}.review.json'
    saved = outbox / 'saved.json'
    review.rename(saved)
    review.symlink_to(saved)
    with pytest.raises((ValueError, OSError)):
        accept(case, source, final)
    assert snapshot(db, owner.root) == before
    assert not list(ledger.iterdir())


@pytest.mark.parametrize('fault', ['wal', 'shm', 'journal', 'header'])
def test_sqlite_guard_precedes_any_sqlite_open(case, monkeypatch, fault):
    api, owner, db, manifest, runtime, inbox, *_ = case
    source = publish(case)
    bundle = api.read_proposal(inbox, source)
    final = api.prepare_review(bundle, actor='operator', warning_ids=[w.warning_id for w in manifest.payload.warnings])
    if fault == 'header':
        with db.open('r+b') as stream:
            stream.seek(18)
            stream.write(b'\x02\x02')
    else:
        db.with_name(db.name + '-' + fault).write_bytes(b'preserve unknown sidecar')
    before = snapshot(db, owner.root)
    sidecars = {p: p.read_bytes() for p in db.parent.glob(db.name + '-*')}
    monkeypatch.setattr(api, '_read_current', lambda *a, **k: pytest.fail('unsafe SQLite open'))
    with pytest.raises(ValueError, match='SQLite'):
        api.check_proposal(bundle, final, database=db, library_root=owner.root)
    assert snapshot(db, owner.root) == before
    assert {p: p.read_bytes() for p in sidecars} == sidecars


def test_cli_has_no_physical_execution_switch():
    from app.tools.reorganization_approval import main
    with pytest.raises(SystemExit) as exc:
        main(['execute'])
    assert exc.value.code == 2


def test_original_manifest_content_hash_is_verified_against_host_identity(case):
    import json
    api, owner, db, manifest, runtime, inbox, outbox, ledger, lock = case
    source = publish(case)
    final = approve(case, source)
    path = inbox / f'{source}.proposal.json'
    bundle = json.loads(path.read_text())
    bundle['manifest'] = manifest_to_json(replace(manifest, created_at='2026-10-19T11:59:59Z'))
    path.write_text(json.dumps(bundle))
    before = snapshot(db, owner.root)
    with pytest.raises(ValueError, match='original canonical manifest hash'):
        accept(case, source, final)
    assert snapshot(db, owner.root) == before
    assert not list(ledger.iterdir())
