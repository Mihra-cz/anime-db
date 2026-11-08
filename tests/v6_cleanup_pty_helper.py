"""Isolated synthetic CLI fixture for real terminal regression tests."""
from pathlib import Path
import sys
from dataclasses import replace
import builtins
import json

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import conftest  # Configure only in-memory/default test startup before app imports.
from pytest import MonkeyPatch
from test_v6_production import production, SHARE
from test_v6_execution_file_actions import generate
from app import reorganization_approval as api
from app.tools.execution_manifest import _EXTERNAL_EVIDENCE_TYPES
from app.tools.reorganization_approval import main

root, count = Path(sys.argv[1]), int(sys.argv[2])
root.mkdir(mode=0o700, exist_ok=True)
patch = MonkeyPatch()
fixture = production.__wrapped__(root, patch)
owner, database, manifest, runtime = next(fixture)
try:
    for index in range(count):
        (owner.root / ('old/unused-%03d-' % index + 'x' * 24)).mkdir(mode=0o700)
    manifest = generate(owner, database)
    runtime = replace(runtime, snapshot_manifest_id=manifest.manifest_id, db_backup_manifest_id=manifest.manifest_id)
    inbox, outbox, ledger = (root / name for name in ('inbox', 'outbox', 'ledger'))
    for directory in (inbox, outbox, ledger):
        directory.mkdir(mode=0o700)
    source = api.publish_proposal(inbox=inbox, database=database, library_root=owner.root,
        share=SHARE, share_root='/', manifest=manifest, runtime=runtime)
    bundle = api.read_proposal(inbox, source)
    final = api.prepare_review(bundle, actor='operator', warning_ids=[w.warning_id for w in manifest.payload.warnings])
    api.write_review(outbox, source, final, actor='operator')
    api.write_approval(outbox, source, final, actor='operator', approved_sha256=api.sha256(final))
    evidence = database.parent / 'runtime.json'
    evidence.write_text(json.dumps({key: getattr(runtime, key) for key in _EXTERNAL_EVIDENCE_TYPES}))
    evidence.chmod(0o600)
    lock = database.parent / 'runtime.lock';lock.write_bytes(b'');lock.chmod(0o600)
    owner.close()
    if '--no-readline' in sys.argv:
        original_import = builtins.__import__
        def unavailable(name, *args, **kwargs):
            if name == 'readline':
                raise ImportError('simulated unavailable readline')
            return original_import(name, *args, **kwargs)
        builtins.__import__ = unavailable
    status = main(['execute', '--db', str(database), '--library-root', str(owner.root),
        '--inbox', str(inbox), '--outbox', str(outbox), '--ledger', str(ledger), '--share', SHARE,
        '--runtime-evidence', str(evidence), '--windows-root', final.payload.windows_root,
        '--source-sha256', source, '--approve-sha256', api.sha256(final), '--actor', 'operator',
        '--runtime-lock', str(lock), '--writers-stopped'])
    result_path = inbox / (source + '.result.json')
    result = json.loads(result_path.read_text()) if result_path.exists() else None
    (root / 'test-outcome.json').write_text(json.dumps({'status': status, 'result': result,
        'remaining': len(list((owner.root / 'old').glob('unused-*'))) if (owner.root / 'old').exists() else 0,
        'readline_loaded': 'readline' in sys.modules, 'claims': len(list(ledger.iterdir()))}))
    raise SystemExit(status)
finally:
    fixture.close()
    patch.undo()
