"""Review regressions: fresh runtime admission and read-only pre-execution checks."""
from dataclasses import asdict, replace
import json

import pytest

from app import reorganization_approval as approval
from app.tools import reorganization_worker as worker
from test_reorganization_approval import case, production, publish, approve, digest, snapshot
from test_reorganization_worker import config_for, request_prepare


def receipt(config, request_id, stage, runtime):
    approval.publish_record(config.ledger, request_id + '.' + stage + '-runtime.json', approval._json(asdict(runtime)))


def start(case, source, final):
    approval.publish_record(case[6], source + '.start-request.json', approval._json(dict(version=1,kind='start',
        request_id=source,source_sha256=source,approved_sha256=digest(final),manifest_id=final.manifest_id,
        actor='operator',requested_at=approval.now())))


def test_static_flags_are_not_runtime_proof(case):
    config = config_for(case)
    request = request_prepare(case)
    with pytest.raises(ValueError, match='runtime|capabilit'):
        worker.plan(config,request)
    assert not list(config.backups.iterdir())


@pytest.mark.parametrize('field', ['scanner_paused_through_verification','inventory_writer_paused_through_verification',
    'safe_write_capability_proven','rename_noreplace_capability_proven','mount_read_write_checked'])
def test_runtime_uses_actual_current_evidence_fields(case, field):
    config=config_for(case); request=request_prepare(case)
    receipt(config,request,'plan',replace(case[4],**{field:False}))
    with pytest.raises(ValueError): worker.plan(config,request)
    assert not list(config.backups.iterdir())


def test_check_start_is_read_only_and_does_not_need_runtime_exclusion(case):
    config=config_for(case); source=publish(case); final=approve(case,source); start(case,source,final)
    before=snapshot(config.database,config.library_root)
    from app.tools.reorganize import runtime_exclusion
    with runtime_exclusion(config.runtime_lock):
        checked=worker.check_start(config,source)
    assert checked==dict(state='READY',source_sha256=source,approved_sha256=digest(final),manifest_id=final.manifest_id)
    assert snapshot(config.database,config.library_root)==before
    assert not list(config.ledger.iterdir())


@pytest.mark.parametrize('fault', ['expired','database','approval'])
def test_readonly_start_check_rejects_before_execution_claim(case,fault):
    config=config_for(case);source=publish(case);final=approve(case,source);start(case,source,final)
    if fault=='expired':
        path=config.inbox/(source+'.proposal.json'); data=json.loads(path.read_text());data['published_at']='2000-01-01T00:00:00Z';path.write_text(json.dumps(data))
    elif fault=='database':
        import sqlite3
        with sqlite3.connect(config.database) as connection: connection.execute('UPDATE videos SET filename=filename || \' changed\' WHERE id=100')
    else:
        path=config.outbox/(source+'.approval.json');data=json.loads(path.read_text());data['approved_sha256']='0'*64;path.write_text(json.dumps(data))
    before=snapshot(config.database,config.library_root)
    with pytest.raises(ValueError):worker.check_start(config,source)
    assert snapshot(config.database,config.library_root)==before
    assert not list(config.ledger.iterdir())


def test_check_idle_is_positive_only_without_claim_or_execution_evidence(case):
    config=config_for(case);source=publish(case);final=approve(case,source);start(case,source,final)
    assert worker.check_idle(config,source)['state']=='E7_NOT_STARTED'
    approval.write_record(config.ledger,final.manifest_id+'.accepted.json','{"uncertain":true}')
    with pytest.raises(ValueError):worker.check_idle(config,source)


def test_worker_stderr_exposes_only_enum_as_machine_category(monkeypatch,capsys):
    monkeypatch.setattr(worker.WorkerConfig,'load',lambda _:object())
    monkeypatch.setattr(worker,'execute',lambda *args:(_ for _ in ()).throw(ValueError('bound_snapshot_not_proven secret /private token=hidden')))
    assert worker.main(['--config','/tmp/config','execute','--request-id','a'*64])==2
    diagnostic=capsys.readouterr().err
    line=next(line for line in diagnostic.splitlines() if line.startswith('STOP_JSON:'))
    parsed=json.loads(line.removeprefix('STOP_JSON:'))
    assert parsed['version']==1 and parsed['error_code']=='SNAPSHOT_FAILED'
    assert 'secret' in parsed['detail']  # This stream is host-private, never public.


def test_execution_claim_rechecks_expiry_after_preflight(case,monkeypatch):
    source=publish(case);final=approve(case,source)
    original=approval.check_proposal
    def expires(bundle, *args, **kwargs):
        result=original(bundle,*args,**kwargs)
        bundle['published_at']='2000-01-01T00:00:00Z'
        return result
    monkeypatch.setattr(approval,'check_proposal',expires)
    from test_reorganization_approval import accept
    with pytest.raises(ValueError,match='expired'):accept(case,source,final)
    assert not list(case[7].iterdir())


def test_publication_starts_review_window_after_preflight(case,monkeypatch):
    from datetime import datetime,timedelta,timezone
    before=datetime.now(timezone.utc)-timedelta(seconds=2)
    after=before+timedelta(seconds=1)
    timestamps=iter([before.isoformat(),after.isoformat()])
    monkeypatch.setattr(approval,'now',lambda:next(timestamps))
    source=publish(case)
    assert approval.read_proposal(case[5],source)['published_at']==after.isoformat()


@pytest.mark.parametrize('evidence',['acceptance','journal','guard','sidecar'])
def test_idle_check_refuses_partial_or_uncertain_execution_evidence(case,evidence):
    config=config_for(case);source=publish(case);final=approve(case,source);start(case,source,final)
    target={'acceptance':config.inbox/(source+'.acceptance.json'),
        'journal':config.database.with_suffix('.journal.jsonl'),
        'guard':config.database.with_name(config.database.name+'.execution-guard.json'),
        'sidecar':config.database.with_name(config.database.name+'-journal')}[evidence]
    target.write_bytes(b'partial uncertain evidence')
    try:worker.check_idle(config,source)
    except Exception as error:
        assert worker.error_code(error)=='EVIDENCE_UNCERTAIN'
    else:pytest.fail('uncertain evidence allowed app restart')


def test_online_start_rechecks_expiry_before_returning_ready(case,monkeypatch):
    config=config_for(case);source=publish(case);final=approve(case,source);start(case,source,final)
    original=approval.check_proposal
    def expires(bundle,*args,**kwargs):
        result=original(bundle,*args,**kwargs)
        bundle['published_at']='2000-01-01T00:00:00Z'
        return result
    monkeypatch.setattr(approval,'check_proposal',expires)
    with pytest.raises(ValueError,match='expired'):worker.check_start(config,source)
    assert not list(config.ledger.iterdir())
