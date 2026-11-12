"""Web transport for the live E7H callback; the original cleanup owns all I/O.

Only the host coordinator exposes this private mailbox to the service edge.
A transport record cannot replace a proposal or authorize a different run.
"""
from dataclasses import asdict
from pathlib import Path
import threading

from . import reorganization_approval as approval
from .target_execution_cleanup import approve_cleanup
from .target_execution_manifest import _strict_json_load


class WebCleanupDecision:
    def __init__(self, *, database, ledger, source_sha256, approved_sha256,
                 stop_event=None, poll_seconds=.25):
        self.database, self.ledger = Path(database), Path(ledger)
        self.source = approval.identity(source_sha256)
        self.approved = approval.identity(approved_sha256)
        self.stop_event = stop_event or threading.Event()
        self.poll_seconds = poll_seconds

    def __call__(self, proposal):
        if proposal.manifest_sha256 != self.approved:
            raise ValueError('cleanup manifest differs from approved run')
        # Same exact diagnostic proposal as the terminal adapter. Never replace
        # old evidence, including evidence from an interrupted prior process.
        approval.write_record(self.database.parent,
            self.database.with_suffix('.cleanup-proposal.json').name, approval._json(asdict(proposal)))
        approval.publish_record(self.ledger, self.source + '.cleanup-request.json', approval._json({
            'source_sha256': self.source, 'approved_sha256': self.approved, 'proposal': asdict(proposal)}))
        while True:
            if self.stop_event.is_set():
                raise ValueError('STOP: cleanup decision transport stopped')
            try:
                stopped = _strict_json_load(approval.read_record(self.ledger,
                    self.source + '.cleanup-stop.json'))
            except FileNotFoundError:
                stopped = None
            if stopped is not None:
                if (type(stopped) is not dict or stopped.get('version') != 1
                        or stopped.get('source_sha256') != self.source):
                    raise ValueError('STOP: cleanup transport evidence is invalid')
                raise ValueError('STOP: coordinator could not safely publish cleanup evidence')
            try:
                raw = approval.read_record(self.ledger, self.source + '.cleanup-decision.json')
            except FileNotFoundError:
                self.stop_event.wait(self.poll_seconds)
                continue
            decision = _strict_json_load(raw)
            required = {'source_sha256', 'approved_sha256', 'proposal_sha256', 'actor', 'directories', 'confirm'}
            if (type(decision) is not dict or not required <= decision.keys()
                    or decision.keys() - required - {'decision', 'csrf'}
                    or decision['source_sha256'] != self.source
                    or decision['approved_sha256'] != self.approved
                    or decision['proposal_sha256'] != proposal.proposal_sha256
                    or decision['confirm'] is not True
                    or type(decision['actor']) is not str or not decision['actor'].strip()
                    or len(decision['actor']) > 128
                    or any(ord(c) < 32 for c in decision['actor'])
                    or type(decision['directories']) is not list
                    or any(type(p) is not str for p in decision['directories'])
                    or decision.get('decision', 'approve') not in {'approve', 'stop'}):
                raise ValueError('STOP: exact one-use web cleanup approval differs')
            if decision.get('decision') == 'stop':
                raise ValueError('STOP: operator declined current cleanup proposal')
            selected = tuple(decision['directories'])
            # None is the existing E7H explicit preserve-all decision. A nonempty
            # selection is validated/sealed by the same approve_cleanup as CLI.
            return approve_cleanup(proposal, selected, actor=decision['actor'],
                reason='operator approved exact live proposal through service edge') if selected else None
