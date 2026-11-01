"""Retained E7H evidence proves only its approved empty-directory delta."""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path

import pytest

from app import target_execution_cleanup as cleanup
from app.target_execution_sequence import ScratchManifestExecution
from app.target_execution_manifest import post_state_hash_from_manifest
from app.target_planner_filesystem import inventory_filesystem
from app.target_planner_service import readonly_planner_session, load_planner_context
from test_v6_execution_cleanup import prepare, run


@pytest.fixture
def completed(tmp_path):
    with ScratchManifestExecution.create(tmp_path) as owner:
        manifest, database = prepare(owner)
        run(owner, manifest, database, lambda proposal: cleanup.approve_cleanup(
            proposal, ('old/copy',), actor='operator', reason='approved empty original leaf'))
        yield owner, manifest, database


def test_completed_cleanup_reopens_and_restores_only_original_hash_projection(completed):
    owner, manifest, database = completed
    outcome = cleanup.read_completed_cleanup(database, manifest, owner.root)
    assert outcome == owner.cleanup_outcome
    assert outcome.removed == ('old/copy',)
    assert not (owner.root / 'old/copy').exists()
    assert (owner.root / 'old/OST').is_dir()
    snapshot = cleanup.restore_cleanup_directories(inventory_filesystem(owner.root), outcome)
    with readonly_planner_session(database) as session:
        context = load_planner_context(session)
    assert post_state_hash_from_manifest(manifest, context, snapshot) == manifest.payload.expected_post_hash
    assert cleanup.cleanup_evidence_digest(database) == hashlib.sha256(owner.cleanup_evidence_path.read_bytes()).hexdigest()


@pytest.mark.parametrize('path', ['IMPORT', 'Import/batch', 'Duplicates', 'Duplicates/old',
    'Subs', 'Subs/old', '#recycle', '@eaDir', 'Show/@eaDir'])
def test_even_resealed_proposal_cannot_authorize_protected_directory(tmp_path, path):
    with ScratchManifestExecution.create(tmp_path) as owner:
        manifest, database = prepare(owner)
        def decide(proposal):
            candidate = cleanup.CleanupCandidate(path, (1, 2), 'UNRESOLVED_EMPTY')
            forged = replace(proposal, candidates=proposal.candidates + (candidate,), proposal_sha256='')
            forged = replace(forged, proposal_sha256=cleanup._seal(forged, 'proposal_sha256'))
            with pytest.raises(ValueError):
                cleanup.approve_cleanup(forged, (path,), actor='operator', reason='must refuse')
            return None
        run(owner, manifest, database, decide)


def _rewrite(path, rows):
    previous = None
    for sequence, row in enumerate(rows):
        row.pop('sha256', None)
        row['sequence'], row['previous_sha256'] = sequence, previous
        previous = cleanup._digest(row)
        row['sha256'] = previous
    path.write_text(''.join(cleanup._canonical(row) + '\n' for row in rows))


def test_metadata_cleanup_rejects_impossible_intermediate_parent_even_if_resealed(tmp_path):
    with ScratchManifestExecution.create(tmp_path) as owner:
        manifest, database = prepare(owner)
        run(owner, manifest, database, lambda proposal: cleanup.approve_cleanup(proposal,
            ('old', 'old/copy', 'old/OST'), actor='operator', reason='original empty source tree'))
        rows = [json.loads(line) for line in owner.cleanup_evidence_path.read_text().splitlines()]
        first = next(row for row in rows if row['state'] == 'VERIFIED')
        first['parent_after']['nlink'] = 0
        first['delta_sha256'] = cleanup._digest({'path': first['path'], 'parent_after': first['parent_after']})
        _rewrite(owner.cleanup_evidence_path, rows)
        with pytest.raises(ValueError):
            cleanup.read_completed_cleanup(database, manifest, owner.root, verify_filesystem=False)


@pytest.mark.parametrize('damage', ['torn', 'missing_verified', 'extra_removed', 'changed_file',
    'changed_database', 'missing_directory', 'new_file', 'replaced_file', 'bad_approval',
    'lost_original_inventory'])
def test_completed_cleanup_rejects_corrupt_history_or_current_state(completed, damage):
    owner, manifest, database = completed
    path = owner.cleanup_evidence_path
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    if damage == 'torn':
        path.write_bytes(path.read_bytes()[:-1])
    elif damage == 'missing_verified':
        _rewrite(path, [row for row in rows if row['state'] != 'VERIFIED'])
    elif damage == 'extra_removed':
        rows[-1]['removed'].append('old/OST')
        _rewrite(path, rows)
    elif damage == 'bad_approval':
        rows[0]['approval']['actor'] = 'different actor'
        _rewrite(path, rows)
    elif damage == 'lost_original_inventory':
        preparation_path = database.with_suffix('.directories.jsonl')
        preparation = [json.loads(line) for line in preparation_path.read_text().splitlines()]
        del preparation[0]['existing']['old/copy']
        _rewrite(preparation_path, preparation)
    elif damage == 'changed_file':
        target = owner.root / manifest.payload.actions[0].target
        target.write_bytes(b'changed content')
    elif damage == 'replaced_file':
        target = owner.root / manifest.payload.actions[0].target
        content = target.read_bytes()
        target.unlink()
        target.write_bytes(content)
    elif damage == 'changed_database':
        database.write_bytes(database.read_bytes() + b'foreign write')
    elif damage == 'missing_directory':
        (owner.root / 'old/OST').rmdir()
    elif damage == 'new_file':
        (owner.root / 'extra.bin').write_bytes(b'unapproved')
    with pytest.raises((ValueError, OSError)):
        cleanup.read_completed_cleanup(database, manifest, owner.root)


def test_historical_cleanup_does_not_pin_current_normal_database_or_media(completed):
    owner, manifest, database = completed
    database.write_bytes(database.read_bytes() + b'later normal write')
    (owner.root / 'later-file.bin').write_bytes(b'ordinary later file')
    assert cleanup.read_completed_cleanup(database, manifest, owner.root,
        verify_filesystem=False).removed == ('old/copy',)


def test_reading_cleanup_evidence_may_change_access_time(completed):
    owner, _, database = completed
    path = owner.cleanup_evidence_path
    expected = hashlib.sha256(path.read_bytes()).hexdigest()
    os.utime(path, ns=(0, path.stat().st_mtime_ns))
    assert cleanup.cleanup_evidence_digest(database) == expected


def test_live_cleanup_owner_rejects_rechecksummed_approval_replacement(tmp_path, monkeypatch):
    original_run = cleanup._Cleanup.run

    def replaced_after_complete(instance, decision):
        result = original_run(instance, decision)
        rows = [json.loads(line) for line in instance.owner.cleanup_evidence_path.read_text().splitlines()]
        approval = cleanup._decode_type(cleanup.CleanupApproval, rows[0]['approval'])
        altered = replace(approval, actor='replacement operator', approval_sha256='')
        altered = replace(altered, approval_sha256=cleanup._seal(altered, 'approval_sha256'))
        rows[0]['approval'] = cleanup.asdict(altered)
        _rewrite(instance.owner.cleanup_evidence_path, rows)
        # A loaded outcome would still contain identical removed paths/snapshots.
        # The live acknowledged record must detect the replacement before release.
        with pytest.raises((ValueError, OSError)):
            instance.owner._cleanup_verifier._check()
        return result

    monkeypatch.setattr(cleanup._Cleanup, 'run', replaced_after_complete)
    with ScratchManifestExecution.create(tmp_path) as owner:
        manifest, database = prepare(owner)
        run(owner, manifest, database, lambda proposal: cleanup.approve_cleanup(
            proposal, ('old/copy',), actor='operator', reason='approved leaf'))
