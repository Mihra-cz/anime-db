"""Namespace index must preserve the complete pre-index planner result."""
from dataclasses import replace

import pytest

from app import target_planner_filesystem as planner
from app.target_planner_types import FilesystemEntry, FilesystemSnapshot, PlanRecord
from target_planner_namespace_reference import _namespace_collisions as reference
from test_target_planner import context


def synthetic_evidence(auxiliary, managed=100):
    ctx = context()
    videos = tuple(replace(ctx.videos[0], id=i+100, source=f'old/video{i}.mkv', episode_number=i+1)
                   for i in range(managed))
    ctx = replace(ctx, videos=videos)
    paths = [v.source for v in videos] + [f'old/Extras/Bundle{i}/asset.flac' for i in range(auxiliary)]
    snapshot = FilesystemSnapshot('/local/synthetic', tuple(FilesystemEntry(p, 'regular', 1, 1) for p in paths), 255, 4096)
    return ctx, snapshot


@pytest.mark.parametrize('auxiliary', [0, 190, 950])
def test_full_plan_equals_pre_index_algorithm(monkeypatch, auxiliary):
    ctx, snapshot = synthetic_evidence(auxiliary)
    current = planner.plan_library(ctx, snapshot)
    monkeypatch.setattr(planner, '_namespace_collisions', reference)
    previous = planner.plan_library(ctx, snapshot)
    assert current == previous  # Includes plan_hash, ordered records, collisions and diagnostics.


def test_prefix_collision_owners_and_order_are_identical():
    records = [
        PlanRecord('video', 1, 'a', 'Show/Extras/OST/video.mkv', 'MOVE', 'READY'),
        PlanRecord('subtitle', 2, 'b', 'show/extras/ost/sub.ass', 'MOVE', 'READY'),
        PlanRecord('video', 3, 'c', 'Show/Season 01/file.mkv', 'MOVE', 'READY'),
        PlanRecord('auxiliary', 'd', 'd', 'Show/Extras/OST/art.jpg', 'MOVE', 'READY', auxiliary_namespace='SHOW/Extras/OST'),
        PlanRecord('auxiliary', 'e', 'e', 'Show/Extras/art.jpg', 'MOVE', 'READY', auxiliary_namespace='Show/Extras'),
        PlanRecord('auxiliary', 'f', 'f', 'Show/a', 'MOVE', 'READY', auxiliary_namespace='Show'),
        PlanRecord('auxiliary', 'g', 'g', 'Show/Extras/OST/Deep/file.jpg', 'MOVE', 'READY', auxiliary_namespace='Show/Extras/OST/Deep'),
    ]
    current, old = records.copy(), records.copy()
    assert planner._namespace_collisions(current) == reference(old)
    assert current == old


def test_prefix_lookup_cost_does_not_multiply_auxiliary_by_managed(monkeypatch):
    ctx, snapshot = synthetic_evidence(190, managed=100)
    real = planner.collision_keys
    calls = 0
    def counted(*args):
        nonlocal calls
        calls += 1
        return real(*args)
    monkeypatch.setattr(planner, 'collision_keys', counted)
    planner.plan_library(ctx, snapshot)
    assert calls < 15000  # Old cross-product adds > 38,000 key normalizations.
