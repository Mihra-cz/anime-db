"""E7E seals the client budget context without adopting any Windows filesystem."""
from dataclasses import FrozenInstanceError, replace
import json
from pathlib import Path

import pytest

from app.target_execution_manifest import (
    build_execution_manifest, manifest_from_json, manifest_to_json,
    post_state_hash_from_manifest, preflight_execution_manifest,
)
from app.target_planner_filesystem import plan_library
from app.target_planner_post_state import simulate_post_state
from test_v6_execution_manifest import DB, actual_db, fixture, ready_runtime


V1_ID = '353bbe3d7fc9002ddd91965b9f018de7bd63ed8dbfe7b7852166b7da1afff341'
V1_PATH = Path(__file__).parent / 'fixtures' / 'v6_manifest_v1.json'


def bound(ctx, snapshot, root, db=DB):
    return build_execution_manifest(ctx, snapshot,
        plan_library(ctx, snapshot, windows_root=root), db, windows_root=root,
        mount_identity='42:1', created_at='2026-10-07T00:00:00Z')


def test_equal_budget_plans_have_different_sealed_drive_identities():
    ctx, snapshot = fixture()
    left, right = (bound(ctx, snapshot, root) for root in (r'C:\Anime', r'D:\Anime'))
    assert left.payload.plan_hash == right.payload.plan_hash
    assert left.payload.actions == right.payload.actions
    assert left.payload.expected_post_hash == right.payload.expected_post_hash
    assert left.manifest_id != right.manifest_id
    assert left.payload.version == 2 and left.payload.windows_root == r'C:\Anime'
    with pytest.raises(FrozenInstanceError):
        left.payload.windows_root = r'D:\Anime'


@pytest.mark.parametrize('root', [r'C:\Anime', 'C:\\', r'\\server\Anime',
    r'\\server\Anime\Library', 'D:/Anime', r'C:\日本語'])
def test_drive_and_unc_roots_round_trip_exactly(root):
    ctx, snapshot = fixture()
    manifest = bound(ctx, snapshot, root)
    assert manifest_from_json(manifest_to_json(manifest)) == manifest
    assert manifest.payload.windows_root == root


@pytest.mark.parametrize('root', ['', 'Anime', r'C:Anime', 'C:', r'\Anime', '/mnt/anime',
    '\\\\server', '\\\\server\\', '\\\\server\\\\Anime', r'\\?\C:\Anime', r'\\.\C:\Anime',
    r'1:\Anime', r'C:\Anime\..\Other', r'C:\.\Anime', r'C:\An*me', r'C:\An?me',
    r'C:\An<me', r'C:\An|me', r'C:\An"me', r'C:\Anime:stream', 'C:\\Anime ',
    'C:\\Anime.', r'C:\NUL', 'C:\\Ani\x00me', 'C:\\Ani\nme', True, 42])
def test_invalid_root_is_rejected_before_sealing(root):
    ctx, snapshot = fixture()
    with pytest.raises(ValueError, match='Windows root'):
        build_execution_manifest(ctx, snapshot, plan_library(ctx, snapshot), DB, windows_root=root)


def test_unbound_manifest_cannot_borrow_a_binding_from_complete_runtime_evidence(tmp_path):
    ctx, snapshot = fixture()
    db = actual_db(tmp_path)
    manifest = bound(ctx, snapshot, None, db)
    runtime = ready_runtime(tmp_path, manifest.manifest_id, db)
    assert json.loads(manifest_to_json(manifest))['payload']['windows_root'] is None
    result = preflight_execution_manifest(manifest, ctx, snapshot, db, runtime=runtime)
    assert result.outcome == 'NOT_READY'
    assert 'sealed_windows_root_not_bound' in result.diagnostics


def test_tampered_root_breaks_the_seal():
    ctx, snapshot = fixture()
    encoded = json.loads(manifest_to_json(bound(ctx, snapshot, r'C:\Anime')))
    encoded['payload']['windows_root'] = r'D:\Anime'
    with pytest.raises(ValueError, match='manifest hash mismatch'):
        manifest_from_json(json.dumps(encoded))


def test_v1_fixture_retains_historical_canonical_bytes_and_hash():
    historical = V1_PATH.read_text().strip()
    loaded = manifest_from_json(historical)
    assert loaded.payload.version == 1 and loaded.manifest_id == V1_ID
    assert manifest_to_json(loaded) == historical
    assert 'windows_root' not in json.loads(manifest_to_json(loaded))['payload']


def test_v1_cannot_gain_readiness_by_supplying_a_runtime_or_caller_root(tmp_path):
    ctx, snapshot = fixture()
    legacy = manifest_from_json(V1_PATH.read_text())
    runtime = ready_runtime(tmp_path, legacy.manifest_id, actual_db(tmp_path))
    result = preflight_execution_manifest(legacy, ctx, snapshot, DB,
        runtime=runtime, windows_root=r'Z:\Anime')
    assert result.outcome == 'NOT_READY'
    assert 'legacy_windows_binding_unavailable' in result.diagnostics
    assert legacy.manifest_id == V1_ID


@pytest.mark.parametrize('change', ['v1_with_root', 'v2_without_root', 'unknown_version'])
def test_decoder_does_not_upgrade_or_infer_payload_versions(change):
    from app.target_execution_manifest import _digest
    encoded = json.loads(V1_PATH.read_text())
    if change == 'v1_with_root':
        encoded['payload']['windows_root'] = r'C:\Anime'
    else:
        encoded['payload']['version'] = 2 if change == 'v2_without_root' else 3
    encoded['manifest_id'] = _digest(encoded['payload'])
    with pytest.raises(ValueError):
        manifest_from_json(json.dumps(encoded))


@pytest.mark.parametrize('override', [r'D:\Anime', None])
def test_preflight_refuses_caller_override_even_for_an_equal_budget(override, tmp_path):
    ctx, snapshot = fixture()
    db = actual_db(tmp_path)
    manifest = bound(ctx, snapshot, r'Z:\Anime', db)
    runtime = ready_runtime(tmp_path, manifest.manifest_id, db)
    result = preflight_execution_manifest(manifest, ctx, snapshot, db,
        runtime=runtime, windows_root=override)
    assert result.outcome == 'NOT_READY' and 'windows_root_mismatch' in result.diagnostics


@pytest.mark.parametrize('actual,diagnostic', [(r'D:\Anime', 'windows_root_mismatch'),
    (r'\\server\Anime', 'windows_root_mismatch'), ('C:Anime', 'actual_windows_root_invalid'),
    (None, 'actual_windows_root_not_proven')])
def test_runtime_must_prove_the_exact_sealed_root(actual, diagnostic, tmp_path):
    ctx, snapshot = fixture()
    db = actual_db(tmp_path)
    manifest = bound(ctx, snapshot, r'Z:\Anime', db)
    runtime = replace(ready_runtime(tmp_path, manifest.manifest_id, db), actual_windows_root=actual)
    result = preflight_execution_manifest(manifest, ctx, snapshot, db, runtime=runtime)
    assert result.outcome == 'NOT_READY' and diagnostic in result.diagnostics
    assert dict(result.checks)['manifest_projection'] == 'MATCH'


@pytest.mark.parametrize('root', [r'Z:\Anime', r'\\server\Anime'])
def test_generation_fresh_preflight_and_final_projection_use_sealed_context(root, tmp_path):
    ctx, snapshot = fixture()
    db = actual_db(tmp_path)
    manifest = manifest_from_json(manifest_to_json(bound(ctx, snapshot, root, db)))
    runtime = replace(ready_runtime(tmp_path, manifest.manifest_id, db), actual_windows_root=root)
    result = preflight_execution_manifest(manifest, ctx, snapshot, db, runtime=runtime)
    assert result.outcome == 'READY_FOR_EXECUTION', result.diagnostics
    assert dict(result.checks)['manifest_projection'] == 'MATCH'
    projected = simulate_post_state(ctx, snapshot, plan_library(ctx, snapshot, windows_root=root),
        windows_root=root)
    assert post_state_hash_from_manifest(manifest, projected.context, projected.snapshot) == manifest.payload.expected_post_hash
    # CHECKED and unchecked planner evidence really differ; retaining the check
    # catches the original E7A implicit-None bug at both ends of execution.
    unbound = bound(ctx, snapshot, None, db)
    assert unbound.payload.plan_hash != manifest.payload.plan_hash
    assert unbound.payload.expected_post_hash != manifest.payload.expected_post_hash
    for override in (None, r'Q:\Other'):
        with pytest.raises(ValueError, match='Windows root mismatch'):
            post_state_hash_from_manifest(manifest, projected.context, projected.snapshot, windows_root=override)


def test_bound_root_still_enforces_windows_budget(tmp_path):
    ctx, snapshot = fixture()
    root = 'Z:\\' + 'long-root-' * 23
    db = actual_db(tmp_path)
    manifest = bound(ctx, snapshot, root, db)
    runtime = replace(ready_runtime(tmp_path, manifest.manifest_id, db), actual_windows_root=root)
    result = preflight_execution_manifest(manifest, ctx, snapshot, db, runtime=runtime)
    assert result.outcome == 'NOT_READY'
    assert any(code.startswith('windows_path_budget:') for code in result.diagnostics)
