"""Real local E7 with browser decision transport; never mounts or accesses NAS."""
import json
from dataclasses import asdict
from pathlib import Path
import threading

import pytest

from app import reorganization_approval as approval
from test_reorganization_approval import production, case, snapshot, digest


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
def test_original_e7_waits_for_exact_web_cleanup_then_normal_or_stop(case, selection):
    from app.tools.reorganization_worker import plan, publish, execute
    from app.target_execution_guard import NormalOperation, _require_no_evidence
    config = config_for(case)
    if selection == 'all':
        for directory in ('nested-unused', 'nested-unused/a', 'nested-unused/a/b'):
            (config.library_root/directory).mkdir(mode=0o700)
    request = request_prepare(case)
    runtime_receipt(config, request, 'plan', case[4])
    runtime_receipt(config, request, 'publish', case[4])
    binding = plan(config, request)
    source = publish(config, request, dict(snapshot_id='fixture', snapshot_at=approval.now(),
        snapshot_manifest_id=binding['manifest_id']))['source_sha256']
    bundle = approval.read_proposal(config.inbox, source)
    original = approval.manifest_from_json(bundle['manifest'])
    final = approval.prepare_review(bundle, actor='operator', warning_ids=[w.warning_id for w in original.payload.warnings])
    approval.write_review(config.outbox, source, final, actor='operator')
    approval.write_approval(config.outbox, source, final, actor='operator', approved_sha256=digest(final))
    approval.write_record(config.outbox, source + '.start-request.json', approval._json(dict(
        version=1, kind='start', request_id=source, source_sha256=source,
        approved_sha256=digest(final), manifest_id=final.manifest_id, actor='operator', requested_at=approval.now())))
    runtime_receipt(config, source, 'execute', case[4])
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
