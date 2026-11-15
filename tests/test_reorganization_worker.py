"""Real local E7 with browser decision transport; never mounts or accesses NAS."""
import json
from dataclasses import asdict
from pathlib import Path
import threading

import pytest

from app import reorganization_approval as approval
from test_reorganization_approval import production, case, snapshot, digest


def manual_attestation(manifest, request_id='a' * 64, **changes):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    value = dict(provenance='operator_attested', request_id=request_id,
        source_sha256=approval.sha256(manifest), manifest_id=manifest.manifest_id,
        share='//synology.test/Anime', snapshot_id='DSM real snapshot ID',
        snapshot_at=now.isoformat(), snapshot_locked=True,
        windows_smb_disconnected=True, no_other_writers=True,
        keep_disconnected_through_normal=True, actor='operator',
        waiting_since=now.isoformat(), deadline_at=(now + timedelta(seconds=1800)).isoformat(),
        confirmed_at=now.isoformat())
    value.update(changes)
    return value


def test_manual_runtime_uses_attestation_without_machine_snapshot_claim(case):
    from app.target_execution_manifest import RuntimeEvidence, _decode_type
    api, owner, db, manifest, runtime, *_ = case
    raw = asdict(runtime)
    raw.update(snapshot_capable=False, snapshot_id=None, snapshot_at=None, snapshot_manifest_id=None,
        operator_attestation=manual_attestation(manifest))
    manual = _decode_type(RuntimeEvidence, raw)
    source = api.publish_proposal(inbox=case[5], database=db, library_root=owner.root,
        share='//synology.test/Anime', share_root='/', manifest=manifest, runtime=manual)
    bundle = api.read_proposal(case[5], source)
    final = api.prepare_review(bundle, actor='operator', warning_ids=[w.warning_id for w in manifest.payload.warnings])
    assert api.check_proposal(bundle, final, database=db, library_root=owner.root).outcome == 'READY_FOR_EXECUTION'
    assert bundle['runtime']['snapshot_capable'] is False
    assert bundle['runtime']['snapshot_id'] is None
    assert bundle['runtime']['operator_attestation']['provenance'] == 'operator_attested'


@pytest.mark.parametrize('changes', [
    {'manifest_id': '0' * 64}, {'source_sha256': '0' * 64},
    {'snapshot_at': '2020-01-01T00:00:00Z'}, {'snapshot_at': '2099-01-01T00:00:00Z'},
    {'snapshot_locked': False}, {'windows_smb_disconnected': False},
    {'no_other_writers': False}, {'keep_disconnected_through_normal': False},
    {'provenance': 'machine_verified'}, {'confirmed_at': '2099-01-01T00:00:00Z'},
    {'deadline_at': '2020-01-01T00:00:00Z'}, {'waiting_since': '2020-01-01T00:00:00Z'},
])
def test_manual_runtime_rejects_incomplete_or_foreign_attestation(case, changes):
    from app.target_execution_manifest import RuntimeEvidence, _decode_type, _runtime_checks
    manifest, runtime = case[3:5]
    raw = asdict(runtime)
    raw.update(snapshot_capable=False, snapshot_id=None, snapshot_at=None, snapshot_manifest_id=None,
        operator_attestation=manual_attestation(manifest, **changes))
    manual = _decode_type(RuntimeEvidence, raw)
    assert 'operator_attestation_invalid' in _runtime_checks(manifest, manual)


def test_legacy_runtime_remains_strictly_decodable():
    from app.target_execution_manifest import RuntimeEvidence, _decode_type
    raw = asdict(RuntimeEvidence())
    raw.pop('operator_attestation', None)
    assert _decode_type(RuntimeEvidence, raw) == RuntimeEvidence()
    with pytest.raises(ValueError):
        _decode_type(RuntimeEvidence, {**raw, 'unknown': True})


def test_manual_worker_publishes_after_request_age_without_renewing_it(case, monkeypatch):
    from dataclasses import replace
    from datetime import datetime, timedelta
    from app.tools import reorganization_worker as worker
    from app.target_execution_manifest import OperatorAttestation, manifest_from_json
    config = replace(config_for(case), protection_mode='manual_one_shot')
    request_id = request_prepare(case)
    runtime_receipt(config, request_id, 'plan', case[4])
    worker.plan(config, request_id)
    manifest = manifest_from_json(approval.read_record(config.ledger, request_id + '.planned.json'))
    attestation = manual_attestation(manifest, request_id)
    runtime = replace(case[4], snapshot_capable=False, snapshot_id=None, snapshot_at=None,
        snapshot_manifest_id=None, operator_attestation=OperatorAttestation(**attestation))
    runtime_receipt(config, request_id, 'publish', runtime)
    before = (config.outbox / (request_id + '.prepare-request.json')).read_bytes()
    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(seconds=1000)
    monkeypatch.setattr(worker, 'datetime', Later)
    with pytest.raises(worker.WorkerStopped, match='expired'):
        worker.read_request(config, request_id, 'prepare')
    source = worker.publish(config, request_id, attestation)['source_sha256']
    assert source == approval.sha256(manifest)
    assert (config.outbox / (request_id + '.prepare-request.json')).read_bytes() == before
    assert approval.read_proposal(config.inbox, source)['runtime']['operator_attestation'] == attestation


def test_automatic_worker_refuses_operator_runtime(case):
    from dataclasses import replace
    from app.tools import reorganization_worker as worker
    from app.target_execution_manifest import OperatorAttestation
    config = config_for(case)
    request_id = request_prepare(case)
    runtime_receipt(config, request_id, 'plan', replace(case[4],
        operator_attestation=OperatorAttestation(**manual_attestation(case[3]))))
    with pytest.raises(worker.WorkerStopped, match='mode'):
        worker.plan(config, request_id)


@pytest.mark.parametrize('fault', ['automatic_worker', 'start_sha', 'approval_sha', 'execute_attestation', 'execute_automatic'])
def test_manual_start_stops_before_e7_for_mismatched_mode_or_attestation(case, monkeypatch, fault):
    from dataclasses import replace
    from app.target_execution_manifest import OperatorAttestation, manifest_from_json
    from app.tools import reorganization_worker as worker
    config = replace(config_for(case), protection_mode='manual_one_shot')
    request = request_prepare(case)
    runtime_receipt(config, request, 'plan', case[4])
    worker.plan(config, request)
    planned = manifest_from_json(approval.read_record(config.ledger, request + '.planned.json'))
    receipt = manual_attestation(planned, request)
    runtime = replace(case[4], snapshot_capable=False, snapshot_id=None, snapshot_at=None,
        snapshot_manifest_id=None, operator_attestation=OperatorAttestation(**receipt))
    runtime_receipt(config, request, 'publish', runtime)
    source = worker.publish(config, request, receipt)['source_sha256']
    bundle = approval.read_proposal(config.inbox, source)
    final = approval.prepare_review(bundle, actor='operator', warning_ids=[w.warning_id for w in planned.payload.warnings])
    approval.write_review(config.outbox, source, final, actor='operator')
    protection = approval.operator_attestation_sha256(bundle)
    approval.write_approval(config.outbox, source, final, actor='operator', approved_sha256=digest(final),
        operator_attestation_sha256=None if fault == 'approval_sha' else protection)
    approval.write_record(config.outbox, source + '.start-request.json', approval._json(dict(
        version=1, kind='start', request_id=source, source_sha256=source, approved_sha256=digest(final),
        manifest_id=final.manifest_id, actor='operator', requested_at=approval.now(),
        operator_attestation_sha256='0' * 64 if fault == 'start_sha' else protection)))
    current = {'execute_attestation': replace(runtime, operator_attestation=replace(
        runtime.operator_attestation, snapshot_id='another DSM snapshot')),
        'execute_automatic': replace(runtime, operator_attestation=None)}.get(fault, runtime)
    runtime_receipt(config, source, 'execute', current)
    if fault == 'automatic_worker':
        config = replace(config, protection_mode='automatic')
    def admitted(**kwargs):
        raise AssertionError('original E7 admission reached')
    monkeypatch.setattr(worker, 'execute_from_approval', admitted)
    case[1].close()
    before = snapshot(config.database, config.library_root)
    with pytest.raises(ValueError) as stopped:
        worker.execute(config, source)
    expected = {'automatic_worker': None, 'start_sha': 'APPROVAL_INVALID', 'approval_sha': 'APPROVAL_INVALID',
        'execute_attestation': 'WRITERS_NOT_STOPPED', 'execute_automatic': 'WRITERS_NOT_STOPPED'}[fault]
    assert getattr(stopped.value, 'error_code', None) == expected, stopped.value
    assert snapshot(config.database, config.library_root) == before
    assert not (config.ledger / (final.manifest_id + '.accepted.json')).exists()
    assert not (config.inbox / (source + '.acceptance.json')).exists()
    assert not config.database.with_suffix('.journal.jsonl').exists()


def config_for(case):
    from app.tools.reorganization_worker import WorkerConfig
    _, owner, database, manifest, runtime, inbox, outbox, ledger, lock = case
    backups, service = database.parent / 'backups', database.parent / 'service'
    backups.mkdir(mode=0o700)
    service.mkdir(mode=0o755)
    return WorkerConfig(database=database, library_root=owner.root,
        share='//synology.test/Anime', share_root='/', windows_root=manifest.payload.windows_root,
        inbox=inbox, outbox=outbox, ledger=ledger, runtime_lock=lock, backups=backups, service=service,
        mount_read_write_checked=True, safe_write_capability_proven=True,
        rename_noreplace_capability_proven=True)


def runtime_receipt(config, request_id, stage, runtime):
    approval.publish_record(config.ledger, request_id + '.' + stage + '-runtime.json', approval._json(asdict(runtime)))


def request_prepare(case):
    from app.tools.target_plan import database_fingerprint
    request_id = 'a' * 64
    request = dict(version=1, kind='prepare', request_id=request_id,
        actor='operator', requested_at=approval.now(), preview_db_sha256=database_fingerprint(case[2])['sha256'])
    approval.write_record(case[6], request_id + '.prepare-request.json', approval._json(request))
    return request_id


def test_worker_generates_bound_manifest_backup_and_publishes_without_execution(case):
    from app.tools.reorganization_worker import plan, publish
    config = config_for(case)
    request = request_prepare(case)
    before = snapshot(case[2], case[1].root)
    runtime_receipt(config, request, 'plan', case[4])
    runtime_receipt(config, request, 'publish', case[4])
    binding = plan(config, request)
    assert set(binding) == {'manifest_id', 'created_at', 'source_sha256'}
    assert not list(case[5].iterdir())
    receipt = dict(snapshot_id='fixture-snapshot', snapshot_at=approval.now(),
        snapshot_manifest_id=binding['manifest_id'])
    source = publish(config, request, receipt)['source_sha256']
    assert source == binding['source_sha256']
    bundle = approval.read_proposal(case[5], source)
    assert bundle['runtime']['mount_identity'] == case[1].mount_identity
    backup = Path(bundle['runtime']['db_backup_path'])
    assert backup.read_bytes() == case[2].read_bytes() and backup.stat().st_ino != case[2].stat().st_ino
    assert snapshot(case[2], case[1].root) == before
    assert not case[2].with_suffix('.journal.jsonl').exists()


@pytest.mark.parametrize('fault', ['database', 'snapshot', 'capability', 'mount'])
def test_prepare_stops_without_execution_for_invalid_runtime(case, monkeypatch, fault):
    from app.tools.reorganization_worker import plan, publish
    from dataclasses import replace
    config = config_for(case)
    request = request_prepare(case)
    if fault == 'database':
        record = case[6] / (request + '.prepare-request.json')
        data = json.loads(record.read_text()); data['preview_db_sha256'] = '0' * 64
        record.write_text(json.dumps(data))
    if fault == 'capability': config = replace(config, safe_write_capability_proven=False)
    if fault == 'mount':
        monkeypatch.setattr(approval, 'worker_runtime', lambda *a, **kw: (_ for _ in ()).throw(ValueError('RW mount unavailable')))
    before = snapshot(case[2], case[1].root)
    runtime_receipt(config, request, 'plan', case[4])
    runtime_receipt(config, request, 'publish', case[4])
    with pytest.raises(ValueError):
        binding = plan(config, request)
        publish(config, request, dict(snapshot_id='wrong', snapshot_at=approval.now(), snapshot_manifest_id='0' * 64))
    assert snapshot(case[2], case[1].root) == before
    assert not list(case[5].iterdir())


@pytest.mark.parametrize('selection', ['preserve', 'selected', 'all', 'stale', 'decline', 'transport_failure'])
@pytest.mark.parametrize('protection_mode', ['automatic', 'manual_one_shot'])
def test_original_e7_waits_for_exact_web_cleanup_then_normal_or_stop(case, selection, protection_mode):
    from dataclasses import replace
    from app.target_execution_manifest import OperatorAttestation, manifest_from_json
    from app.tools.reorganization_worker import plan, publish, execute
    from app.target_execution_guard import NormalOperation, _require_no_evidence
    config = replace(config_for(case), protection_mode=protection_mode)
    if selection == 'all':
        for directory in ('nested-unused', 'nested-unused/a', 'nested-unused/a/b'):
            (config.library_root/directory).mkdir(mode=0o700)
    request = request_prepare(case)
    runtime_receipt(config, request, 'plan', case[4])
    binding = plan(config, request)
    runtime = case[4]
    receipt = dict(snapshot_id='fixture', snapshot_at=approval.now(), snapshot_manifest_id=binding['manifest_id'])
    if protection_mode == 'manual_one_shot':
        planned = manifest_from_json(approval.read_record(config.ledger, request + '.planned.json'))
        receipt = manual_attestation(planned, request)
        runtime = replace(runtime, snapshot_capable=False, snapshot_id=None, snapshot_at=None,
            snapshot_manifest_id=None, operator_attestation=OperatorAttestation(**receipt))
    runtime_receipt(config, request, 'publish', runtime)
    source = publish(config, request, receipt)['source_sha256']
    bundle = approval.read_proposal(config.inbox, source)
    original = approval.manifest_from_json(bundle['manifest'])
    final = approval.prepare_review(bundle, actor='operator', warning_ids=[w.warning_id for w in original.payload.warnings])
    approval.write_review(config.outbox, source, final, actor='operator')
    approval.write_approval(config.outbox, source, final, actor='operator', approved_sha256=digest(final),
        operator_attestation_sha256=approval.operator_attestation_sha256(bundle))
    protection = ({'operator_attestation_sha256': approval.operator_attestation_sha256(bundle)}
        if protection_mode == 'manual_one_shot' else {})
    approval.write_record(config.outbox, source + '.start-request.json', approval._json(dict(
        version=1, kind='start', request_id=source, source_sha256=source,
        approved_sha256=digest(final), manifest_id=final.manifest_id, actor='operator', requested_at=approval.now(), **protection)))
    runtime_receipt(config, source, 'execute', runtime)
    case[1].close()
    entered = threading.Event()
    # Real worker in another thread: no terminal input, no substituted E7 engine.
    results, errors = [], []
    def worker():
        try: results.append(execute(config, source))
        except BaseException as exc: errors.append(exc)
        finally: entered.set()
    task = threading.Thread(target=worker)
    task.start()
    import time
    deadline = time.monotonic() + 30
    pending = config.ledger / (source + '.cleanup-request.json')
    while not pending.exists() and task.is_alive() and time.monotonic() < deadline:
        entered.wait(.02)
    assert pending.exists(), errors
    live = json.loads(approval.read_record(config.ledger, pending.name))
    proposal = live['proposal']
    assert task.is_alive() and not results
    progress = json.loads((config.service / 'progress.json').read_text())
    assert progress['waiting_for_operator'] and progress['source_sha256'] == source
    selected = [p['path'] for p in proposal['candidates'] if p['path'].startswith('old')] if selection == 'selected' else []
    if selection == 'all':
        selected = [p['path'] for p in proposal['candidates']]
        assert {'nested-unused','nested-unused/a','nested-unused/a/b'} <= set(selected)
    decision = dict(source_sha256=source, approved_sha256=digest(final), proposal_sha256=proposal['proposal_sha256'],
        actor='web operator', directories=selected, confirm=True)
    if selection == 'stale': decision['proposal_sha256'] = '0' * 64
    if selection == 'decline': decision['decision'] = 'stop'
    if selection == 'transport_failure':
        approval.publish_record(config.ledger, source + '.cleanup-stop.json', approval._json(dict(version=1,source_sha256=source,reason='CLEANUP_EVIDENCE_INVALID')))
    else:
        approval.write_record(config.ledger, source + '.cleanup-decision.json', approval._json(decision))
    task.join(30)
    assert not task.is_alive()
    result = approval.read_execution_result(config.inbox, source, final)
    after = snapshot(config.database, config.library_root)
    if selection in {'stale', 'decline', 'transport_failure'}:
        assert errors and result['state'] == 'STOP'
    else:
        assert not errors, errors
        assert results[0]['state'] == 'NORMAL'
        assert result['removed_directories'] == sorted(selected, key=lambda p: (-len(p.split('/')), p))
        assert isinstance(_require_no_evidence(config.database), NormalOperation)
        assert all(not (config.library_root / p).exists() for p in selected)
        if selection == 'preserve': assert (config.library_root / 'old').is_dir()
    with pytest.raises(FileExistsError): execute(config, source)
    assert snapshot(config.database, config.library_root) == after


def test_preparation_requires_same_unowned_runtime_lock(case):
    from app.tools.reorganization_worker import plan
    from app.tools.reorganize import runtime_exclusion
    config = config_for(case)
    request = request_prepare(case)
    before = snapshot(config.database, config.library_root)
    with runtime_exclusion(config.runtime_lock):
        with pytest.raises(Exception, match='runtime.lock'):
            plan(config, request)
    assert snapshot(config.database, config.library_root) == before
    assert not list(config.backups.iterdir()) and not list(config.ledger.iterdir())


def test_queue_publication_never_exposes_incomplete_request(case, monkeypatch):
    outbox = case[6]
    target = outbox / 'atomic-request.json'
    from app import target_execution_journal as journal
    original = journal._write_all
    def writing(fd, raw):
        assert not target.exists()
        original(fd, raw)
        assert not target.exists()
    monkeypatch.setattr(journal, '_write_all', writing)
    approval.publish_record(outbox, target.name, '{"complete":true}')
    assert approval.read_record(outbox, target.name) == '{"complete":true}'
    monkeypatch.setattr(journal, '_write_all', original)
    with pytest.raises(FileExistsError): approval.publish_record(outbox, target.name, '{}')


def test_worker_cli_reports_identity_without_repeating_large_cleanup_evidence(monkeypatch, capsys):
    from app.tools import reorganization_worker as module
    result = dict(state='NORMAL',source_sha256='a'*64,approved_sha256='b'*64,manifest_id='c'*64,
        removed_directories=['日本語の非常に長い元のディレクトリ/'*8+str(i) for i in range(279)])
    monkeypatch.setattr(module.WorkerConfig,'load',lambda _: object())
    monkeypatch.setattr(module,'execute',lambda *args: result)
    assert module.main(['--config','/tmp/private-config.json','execute','--request-id','a'*64]) == 0
    output = capsys.readouterr().out
    assert len(output.encode('utf-8')) < 1024
    assert json.loads(output) == {key:result[key] for key in ('state','source_sha256','approved_sha256','manifest_id')}
