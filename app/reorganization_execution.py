"""Administrative handoff into the original E7, with aggregate result publication.

The original E1/E7 evidence remains the only action history. This adapter neither
moves files nor interprets cleanup proposals; the existing worker owns both.
"""
from pathlib import Path

from . import reorganization_approval as approval
from .tools.reorganize import execute_approved_reorganization, _interactive_cleanup


def execute_from_approval(*, inbox, outbox, ledger, source_sha256, approved_sha256, actor,
                          database, library_root, share, share_root, runtime, runtime_lock,
                          writers_stopped=False, progress=None):
    # The existing create-once ledger consumes even a disabled acceptance. There
    # is no path from ACCEPTED_EXECUTION_DISABLED to this execution claim.
    claim = approval.accept_approval(inbox=inbox, outbox=outbox, ledger=ledger,
        source_sha256=source_sha256, approved_sha256=approved_sha256, actor=actor,
        database=database, library_root=library_root, share=share, share_root=share_root,
        runtime=runtime, runtime_lock=runtime_lock, writers_stopped=writers_stopped, for_execution=True)
    # accept_approval has LEFT runtime_exclusion here. E7 must acquire its own
    # original lock and repeat live admission; never nest the same flock.
    total = None
    latest = {'phase': 'EXECUTION_CLAIMED', 'completed': 0}
    started = False

    def observe(event):
        nonlocal latest, started
        latest = dict(event)
        started = started or 'action_index' in event
        if progress is not None:
            progress(event)

    def publish(state, *, error=None, removed=None):
        record = {**claim, 'state': state, 'started': started, 'finished_at': approval.now(),
            'phase': latest['phase'], 'completed': latest['completed'], 'total': total,
            'remaining': total - latest['completed'],
            'removed_directories': list(removed) if removed is not None else None,
            'error': error, 'journal_path': str(Path(database).with_suffix('.journal.jsonl')),
            'guard_path': str(Path(database).with_name(Path(database).name + '.execution-guard.json')),
            'cleanup_evidence_path': str(Path(database).with_suffix('.cleanup.jsonl'))}
        approval.write_record(inbox, source_sha256 + '.result.json', approval._json(record))
        return record

    try:
        bundle = approval.read_proposal(inbox, source_sha256)
        manifest = approval.read_review(outbox, source_sha256)
        total = len(manifest.payload.actions)
        approval.validate_review(bundle, manifest, actor=actor)
        if approval.sha256(manifest) != approved_sha256 or claim['state'] != 'EXECUTION_CLAIMED':
            raise ValueError('execution claim differs from exact canonical approval')
        result = execute_approved_reorganization(database=database, library_root=library_root,
            share=share, share_root=share_root, manifest=manifest, approved_sha256=approved_sha256,
            runtime=runtime, runtime_lock=runtime_lock, cleanup_decision=_interactive_cleanup(database, actor),
            progress=observe, admission_check=lambda: approval.require_current_proposal(bundle))
    except BaseException as exc:
        # A killed process cannot publish a result: its spent claim and original
        # E7 guard/evidence remain visible as unconfirmed, never restart authority.
        if total is not None:
            if getattr(exc, 'phase', None):
                latest['phase'] = exc.phase
            try:
                publish('STOP', error=f'{type(exc).__name__}: {exc}'[:2048])
            except BaseException as publication_error:
                exc.add_note(f'result publication failed: {type(publication_error).__name__}; preserve all evidence')
        raise
    return publish('NORMAL', removed=result['removed_directories'])
