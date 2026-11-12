"""Only local synthetic libraries: approval -> original E7/E7H -> RO report."""
from dataclasses import replace
import json
import time

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app import reorganization_approval as api
from test_reorganization_approval import production, case, publish, approve, accept, snapshot, digest
from test_reorganization_approval_web import web, warning_free_web, csrf


def execution_args(case, source, final):
    from test_v6_production import SHARE
    _, owner, db, manifest, runtime, inbox, outbox, ledger, lock = case
    return dict(inbox=inbox, outbox=outbox, ledger=ledger, source_sha256=source,
        approved_sha256=digest(final), actor='operator', database=db, library_root=owner.root,
        share=SHARE, share_root='/', runtime=runtime, runtime_lock=lock, writers_stopped=True)



def test_complete_host_execution_live_cleanup_normal_ro_restart(web, monkeypatch):
    from app.reorganization_execution import execute_from_approval
    from app import target_execution_mounts as mounts
    from v6_database_case import database_rows
    from test_v6_execution_projection import expected_rows
    case, client, source = web
    _, owner, db, manifest, runtime, inbox, outbox, ledger, lock = case
    start = time.monotonic()
    path = f'/reorganization/approval/{source}'
    page = client.get(path)
    prepared = client.post(path + '/prepare', data={'csrf': csrf(page), 'actor': 'operator',
        'warnings': [w.warning_id for w in manifest.payload.warnings]}, headers={'origin': 'http://testserver'})
    final = api.read_review(outbox, source)
    approved = client.post(path + '/approve', data={'csrf': csrf(prepared), 'actor': 'operator',
        'sha256': digest(final), 'confirm': 'yes'}, headers={'origin': 'http://testserver'})
    assert approved.status_code == 200
    assert {a.action for a in final.payload.actions} == {'MOVE', 'KEEP', 'QUARANTINE'}
    rows = database_rows(db)
    files = {a.target: (owner.root / a.source).read_bytes() for a in final.payload.actions}
    protected = (owner.root / 'IMPORT/uploading.mkv').read_bytes()
    selected = []
    def input_cleanup(prompt):
        proposal = json.loads(db.with_suffix('.cleanup-proposal.json').read_text())
        if prompt.startswith('Approve'):
            assert not any((owner.root / a.source).exists() for a in final.payload.actions if a.action == 'MOVE')
            selected.extend(p['path'] for p in proposal['candidates'] if p['path'] == 'old' or p['path'].startswith('old/'))
            assert selected
            assert all(not p.startswith(('IMPORT', 'Duplicates', 'Subs')) for p in selected)
            return json.dumps(selected)
        return proposal['proposal_sha256']
    monkeypatch.setattr('builtins.input', input_cleanup)
    owner.close()
    progress = []
    # Original E3/E7 hashes use pread; metadata rename must never read video payload.
    import os
    original_pread = os.pread
    def metadata_only_read(fd, count, offset):
        assert not os.readlink(f'/proc/self/fd/{fd}').lower().endswith(('.mkv', '.mp4', '.avi', '.m4v'))
        return original_pread(fd, count, offset)
    from app.reorganization_service import ServiceProgress
    public = db.parent / 'service'; public.mkdir(mode=0o755)
    with monkeypatch.context() as reads, ServiceProgress(public, library_root=owner.root) as service:
        reads.setattr(os, 'pread', metadata_only_read)
        def observe(event):
            progress.append(event)
            service.observe(event)
        result = execute_from_approval(**execution_args(case, source, final), progress=observe)
        service.finish('NORMAL')
    status = json.loads((public / 'progress.json').read_text())
    assert status['outcome'] == 'NORMAL' and not status['active']
    assert {row['state'] for row in status['phases']} == {'PASS'}
    assert status['completed'] == status['rename_completed'] == len(final.payload.actions)
    from app.target_execution_journal import load_journal
    entries = load_journal(db.with_suffix('.journal.jsonl'), final).entries
    assert status['completed'] == sum(entry.state == 'COMPLETE' for entry in entries)
    assert status['rename_completed'] == sum(entry.state == 'FS_VERIFIED' for entry in entries)
    assert result['state'] == 'NORMAL'
    assert result['completed'] == len(final.payload.actions) and result['remaining'] == 0
    assert any(p['phase'] == 'WAITING_FOR_CLEANUP_APPROVAL' for p in progress)
    assert all(result['total'] == p['completed'] + p['remaining'] for p in progress)
    assert {a.target: (owner.root / a.target).read_bytes() for a in final.payload.actions} == files
    assert all(not (owner.root / p).exists() for p in selected)
    assert (owner.root / 'IMPORT/uploading.mkv').read_bytes() == protected
    assert database_rows(db) == expected_rows(rows, final.payload.locator_patches)
    from app.target_execution_guard import _require_no_evidence, NormalOperation
    assert isinstance(_require_no_evidence(db), NormalOperation)
    current = mounts._check_production_mount
    monkeypatch.setattr(mounts, '_check_production_mount', lambda *a, **kw:
        replace(current(*a, **kw), options=('ro',)) if not kw['writable'] else current(*a, **kw))
    before = snapshot(db, owner.root)
    settings = Settings(anime_path=owner.root, database_url=f'sqlite:///{db}',
        metadata_artwork_directory=db.parent / 'artwork', metadata_download_artwork=False,
        reorganization_approval_inbox=inbox, reorganization_approval_outbox=outbox)
    with TestClient(create_app(settings)) as restarted:
        report = restarted.get(path)
        assert report.status_code == 200 and 'NORMAL' in report.text
        assert 'Výsledek reorganizace' in report.text and digest(final) in report.text
        assert 'Spuštěno: ano' in report.text
    assert snapshot(db, owner.root) == before
    assert api.read_execution_result(inbox, source, final)['state'] == 'NORMAL'
    with pytest.raises(FileExistsError):
        execute_from_approval(**execution_args(case, source, final))
    assert snapshot(db, owner.root) == before
    print(f'local approval through NORMAL + RO restart: {time.monotonic()-start:.3f}s')


def test_disabled_acceptance_never_becomes_execution_authority(case):
    from app.reorganization_execution import execute_from_approval
    source = publish(case)
    final = approve(case, source)
    accept(case, source, final)
    before = snapshot(case[2], case[1].root)
    with pytest.raises(FileExistsError):
        execute_from_approval(**execution_args(case, source, final))
    assert snapshot(case[2], case[1].root) == before


@pytest.mark.parametrize('fault', ['hash', 'lock', 'expired'])
def test_execution_admission_rejects_without_sqlite_or_media_changes(case, fault):
    from app.reorganization_execution import execute_from_approval
    source = publish(case)
    final = approve(case, source)
    args = execution_args(case, source, final)
    if fault == 'hash': args['approved_sha256'] = '0' * 64
    elif fault == 'expired':
        p = case[5] / f'{source}.proposal.json'
        bundle = json.loads(p.read_text());bundle['published_at'] = '2020-01-01T00:00:00Z'
        p.write_text(json.dumps(bundle))
    from app.tools.reorganize import runtime_exclusion
    from contextlib import nullcontext
    with runtime_exclusion(case[-1]) if fault == 'lock' else nullcontext():
        before = snapshot(case[2], case[1].root)
        with pytest.raises(Exception):
            execute_from_approval(**args)
    assert snapshot(case[2], case[1].root) == before
    assert not list(case[7].iterdir())


def test_warning_free_actor_survives_prg_get(warning_free_web):
    case, client, source = warning_free_web
    path = f'/reorganization/approval/{source}'
    response = client.post(path + '/prepare', data={'csrf': csrf(client.get(path)), 'actor': 'Mirek'},
        headers={'origin': 'http://testserver'}, follow_redirects=False)
    assert response.status_code == 303 and response.headers['location'] == path
    page = client.get(path)
    assert 'name="actor" value="Mirek"' in page.text
    page = client.get(path)
    assert 'name="actor" value="Mirek"' in page.text
    final = api.read_review(case[6], source)
    response = client.post(path + '/approve', data={'csrf': csrf(page), 'actor': 'Mirek',
        'sha256': digest(final), 'confirm': 'yes'}, headers={'origin': 'http://testserver'}, follow_redirects=False)
    assert response.status_code == 303
    assert api.read_approval(case[6], source)['actor'] == 'Mirek'


def test_csrf_failure_is_html_stop_page_without_writes(web):
    case, client, source = web
    response = client.post(f'/reorganization/approval/{source}/prepare', data={})
    assert response.status_code == 403
    assert response.headers['content-type'].startswith('text/html')
    assert 'STOP' in response.text and 'Origin' in response.text
    assert not list(case[6].iterdir())


@pytest.mark.parametrize('phase', ['PENDING', 'COMPLETE', 'cleanup'])
def test_interruption_and_cleanup_eof_publish_stop_without_retry(case, monkeypatch, phase):
    from app.reorganization_execution import execute_from_approval
    source = publish(case)
    final = approve(case, source)
    owner, db = case[1], case[2]
    owner.close()
    from app.reorganization_service import ServiceProgress
    public = db.parent / 'service'; public.mkdir(mode=0o755)
    before = snapshot(db, owner.root)
    def interrupt(event):
        service.observe(event)
        if event['phase'] == phase:
            raise KeyboardInterrupt()
    def eof(_prompt):
        raise EOFError('administrator disconnected during live cleanup')
    monkeypatch.setattr('builtins.input', eof)
    with pytest.raises((KeyboardInterrupt, Exception)), ServiceProgress(public, library_root=owner.root) as service:
        execute_from_approval(**execution_args(case, source, final), progress=interrupt)
    result = api.read_execution_result(case[5], source, final)
    assert result['state'] == 'STOP'
    status = json.loads((public / 'progress.json').read_text())
    assert status['outcome'] == 'STOP' and not status['active']
    assert status['completed'] == result['completed']
    from app.target_execution_journal import load_journal
    entries = load_journal(db.with_suffix('.journal.jsonl'), final).entries
    assert status['completed'] == sum(entry.state == 'COMPLETE' for entry in entries)
    assert status['rename_completed'] == sum(entry.state == 'FS_VERIFIED' for entry in entries)
    assert 'STOP' in {row['state'] for row in status['phases']}
    assert result['completed'] == (0 if phase == 'PENDING' else 1 if phase == 'COMPLETE' else len(final.payload.actions))
    if phase == 'PENDING':
        assert snapshot(db, owner.root) == before
    if phase == 'cleanup':
        assert result['phase'] == 'CLEANUP'
        assert db.with_suffix('.cleanup-proposal.json').exists()
    after = snapshot(db, owner.root)
    with pytest.raises(FileExistsError):
        execute_from_approval(**execution_args(case, source, final))
    assert snapshot(db, owner.root) == after


def test_expiry_rechecked_after_admission_lock_is_released(case, monkeypatch):
    from app.reorganization_execution import execute_from_approval
    source = publish(case)
    final = approve(case, source)
    # Publication age is checked before and after preflight, then at E7 dispatch.
    original = api.require_current_proposal
    calls = []
    def expire_at_dispatch(bundle):
        calls.append(1)
        if len(calls) == 3:
            raise ValueError('host proposal expired')
        return original(bundle)
    monkeypatch.setattr(api, 'require_current_proposal', expire_at_dispatch)
    before = snapshot(case[2], case[1].root)
    with pytest.raises(ValueError, match='expired'):
        execute_from_approval(**execution_args(case, source, final))
    assert len(calls) == 3
    assert snapshot(case[2], case[1].root) == before
    assert api.read_execution_result(case[5], source, final)['state'] == 'STOP'


def test_execute_requires_terminal_before_claim(case, monkeypatch, capsys):
    from app.tools.reorganization_approval import main
    from test_v6_production import SHARE
    source = publish(case)
    final = approve(case, source)
    monkeypatch.setattr('sys.stdin.isatty', lambda: False)
    result = main(['execute', '--db', str(case[2]), '--library-root', str(case[1].root),
        '--inbox', str(case[5]), '--outbox', str(case[6]), '--ledger', str(case[7]),
        '--share', SHARE, '--runtime-evidence', str(case[2].parent / 'unused.json'),
        '--windows-root', final.payload.windows_root, '--source-sha256', source,
        '--approve-sha256', digest(final), '--actor', 'operator', '--runtime-lock', str(case[-1]), '--writers-stopped'])
    assert result == 2 and 'interactive' in capsys.readouterr().err
    assert not list(case[7].iterdir())


def test_missing_result_after_claim_is_unconfirmed_and_never_auto_runs(web):
    case, client, source = web
    path = f'/reorganization/approval/{source}'
    page = client.get(path)
    prepared = client.post(path + '/prepare', data={'csrf': csrf(page), 'actor': 'operator',
        'warnings': [w.warning_id for w in case[3].payload.warnings]}, headers={'origin': 'http://testserver'})
    final = api.read_review(case[6], source)
    client.post(path + '/approve', data={'csrf': csrf(prepared), 'actor': 'operator',
        'sha256': digest(final), 'confirm': 'yes'}, headers={'origin': 'http://testserver'})
    api.accept_approval(**execution_args(case, source, final), for_execution=True)
    before = snapshot(case[2], case[1].root)
    page = client.get(path)
    assert page.status_code == 200
    assert 'výsledek zatím nepotvrzený' in page.text
    assert 'Spuštěno: ano' not in page.text
    assert snapshot(case[2], case[1].root) == before


def test_cli_executes_original_e7_and_prints_terminal_progress(case, monkeypatch, capsys):
    from app.tools.reorganization_approval import main
    from app.tools.execution_manifest import _EXTERNAL_EVIDENCE_TYPES
    from test_v6_production import SHARE
    source = publish(case)
    final = approve(case, source)
    runtime_file = case[2].parent / 'runtime.json'
    runtime_file.write_text(json.dumps({key: getattr(case[4], key) for key in _EXTERNAL_EVIDENCE_TYPES}))
    runtime_file.chmod(0o600)
    monkeypatch.setattr('sys.stdin.isatty', lambda: True)
    monkeypatch.setattr('sys.stdout.isatty', lambda: True)
    def decide(prompt):
        proposal = json.loads(case[2].with_suffix('.cleanup-proposal.json').read_text())
        return json.dumps([p['path'] for p in proposal['candidates'] if p['path'].startswith('old')]) if prompt.startswith('Approve') else proposal['proposal_sha256']
    monkeypatch.setattr('builtins.input', decide)
    case[1].close()
    public = case[2].parent / 'service'; public.mkdir(mode=0o755)
    status = main(['execute', '--db', str(case[2]), '--library-root', str(case[1].root),
        '--inbox', str(case[5]), '--outbox', str(case[6]), '--ledger', str(case[7]),
        '--share', SHARE, '--runtime-evidence', str(runtime_file), '--windows-root', final.payload.windows_root,
        '--source-sha256', source, '--approve-sha256', digest(final), '--actor', 'operator',
        '--runtime-lock', str(case[-1]), '--writers-stopped', '--service-status-dir', str(public)])
    assert status == 0
    summary = json.loads((public / 'progress.json').read_text())
    assert summary['outcome'] == 'NORMAL' and not summary['active']
    terminal = capsys.readouterr()
    assert 'WAITING_FOR_CLEANUP_APPROVAL' in terminal.err and 'NORMAL' in terminal.err
    assert f"dokončeno {len(final.payload.actions)}/{len(final.payload.actions)}" in terminal.err
    assert 'zbývá 0' in terminal.err
    assert json.loads(terminal.out.splitlines()[-1])['state'] == 'NORMAL'


@pytest.mark.parametrize('field,bad', [('phase', 1), ('actor', None), ('error', {}), ('removed_directories', 'unknown'), ('journal_path', [])])
def test_malformed_aggregate_result_is_stop(case, field, bad):
    source = publish(case)
    final = approve(case, source)
    record = dict(source_sha256=source, approved_sha256=digest(final), manifest_id=final.manifest_id,
        state='NORMAL', started=True, completed=len(final.payload.actions), total=len(final.payload.actions),
        remaining=0, actor='operator', phase='NORMAL', finished_at=api.now(), removed_directories=[],
        journal_path='local journal', guard_path='local guard', cleanup_evidence_path='local cleanup', error=None)
    record[field] = bad
    api.write_record(case[5], source + '.result.json', json.dumps(record))
    with pytest.raises(ValueError, match='result'):
        api.read_execution_result(case[5], source, final)
