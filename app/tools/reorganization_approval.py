"""Explicit administrative approval transport into the original E7.

publish: administrator publishes a worker-bound existing canonical manifest.
accept: administrator independently confirms its final SHA and stopped writers;
        validates runtime.lock, RW CIFS and fresh preflight, then consumes once.
inspect: bounded read-only web helper using dated host runtime evidence.
execute: fresh execution claim and original E7 with live terminal cleanup.
"""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys
import signal
import os

from app import reorganization_approval as approval
from app.target_execution_manifest import RuntimeEvidence, manifest_from_json
from app.tools.execution_manifest import _read_runtime_evidence
from app.tools.reorganize import _read_manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('publish', 'accept', 'inspect', 'execute'):
        p = sub.add_parser(name)
        p.add_argument('--db', type=Path, required=True)
        p.add_argument('--library-root', type=Path, required=True)
        p.add_argument('--inbox', type=Path, required=True)
        if name == 'inspect':
            p.add_argument('--source-sha256', required=True)
            continue
        p.add_argument('--share', required=True)
        p.add_argument('--share-root', default='/')
        p.add_argument('--runtime-evidence', type=Path, required=True)
        p.add_argument('--windows-root', required=True)
        if name == 'publish':
            p.add_argument('--manifest', type=Path, required=True)
        else:
            p.add_argument('--outbox', type=Path, required=True)
            p.add_argument('--ledger', type=Path, required=True)
            p.add_argument('--source-sha256', required=True)
            p.add_argument('--approve-sha256', required=True)
            p.add_argument('--actor', required=True)
            p.add_argument('--runtime-lock', type=Path, required=True)
            p.add_argument('--writers-stopped', action='store_true', required=True)
            if name == 'execute':
                p.add_argument('--service-status-dir', type=Path,
                    default=os.getenv('REORGANIZATION_SERVICE_STATUS_DIR'),
                    help='Optional dedicated local public-summary directory; never execution evidence')
    args = parser.parse_args(argv)
    previous_signal = None
    service = None
    try:
        if args.command == 'execute':
            if not sys.stdin.isatty() or not sys.stdout.isatty():
                raise ValueError('execute requires interactive stdin AND stdout terminals; do not pipe output through tee')
            try:
                import readline  # Installs Python's long-line terminal reader before any approval claim.
            except ImportError as exc:
                raise ValueError('execute requires readline for long explicit E7H cleanup approvals') from exc
            previous_signal = signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
            if args.service_status_dir is not None:
                from app.reorganization_service import ServiceProgress
                service = ServiceProgress(args.service_status_dir, library_root=args.library_root)
                service.__enter__()
        if args.command == 'inspect':
            bundle = approval.read_proposal(args.inbox, args.source_sha256)
            raw = sys.stdin.buffer.read(approval.MAX_RECORD_BYTES + 1)
            if len(raw) > approval.MAX_RECORD_BYTES:
                raise ValueError('manifest too large')
            final = manifest_from_json(raw.decode('utf-8'))
            result = approval.check_proposal(bundle, final, database=args.db, library_root=args.library_root)
            print(json.dumps(asdict(result)))
            return 0
        external = _read_runtime_evidence(args.runtime_evidence, args.library_root)
        runtime = RuntimeEvidence(**external, actual_windows_root=args.windows_root)
        if args.command == 'publish':
            manifest = _read_manifest(args.manifest, args.library_root, args.db)
            source = approval.publish_proposal(inbox=args.inbox, database=args.db, library_root=args.library_root,
                share=args.share, share_root=args.share_root, manifest=manifest, runtime=runtime)
            print(json.dumps({'source_sha256': source, 'execution_enabled': False}))
        else:
            handler = approval.accept_approval
            extra = {}
            if args.command == 'execute':
                from app.reorganization_execution import execute_from_approval
                handler = execute_from_approval
                def progress(event):
                    if service is not None:
                        service.observe(event)
                    print(f"Fáze {event['phase']} · dokončeno {event['completed']}/{event['total']} · "
                          f"zbývá {event['remaining']}", file=sys.stderr, flush=True)
                extra['progress'] = progress
            result = handler(inbox=args.inbox, outbox=args.outbox, ledger=args.ledger,
                source_sha256=args.source_sha256, approved_sha256=args.approve_sha256, actor=args.actor,
                database=args.db, library_root=args.library_root, share=args.share, share_root=args.share_root,
                runtime=runtime, runtime_lock=args.runtime_lock, writers_stopped=args.writers_stopped, **extra)
            if service is not None:
                service.finish('NORMAL')
            print(json.dumps(result))
        return 0
    except (Exception, KeyboardInterrupt, SystemExit) as exc:
        if service is not None:
            service.finish('STOP', error=exc)
        print(f'STOP: {exc}. Preserve all evidence; do not retry, restore or copy automatically. '
              'Manual intervention required.', file=sys.stderr)
        return 130 if isinstance(exc, KeyboardInterrupt) else 143 if isinstance(exc, SystemExit) else 2
    finally:
        if service is not None:
            service.close()
        if previous_signal is not None:
            signal.signal(signal.SIGTERM, previous_signal)


if __name__ == '__main__':
    raise SystemExit(main())
