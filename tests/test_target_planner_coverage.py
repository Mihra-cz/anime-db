"""E7C accounts for the observed library without authorizing execution."""
from dataclasses import asdict, replace
import importlib
import json
import os

import pytest

from app.target_planner_filesystem import inventory_filesystem, plan_library
from app.target_planner_types import FilesystemEntry, FilesystemSnapshot, TargetSubtitle
from test_physical_naming import session
from test_target_planner import context


def coverage(snapshot, plan):
    return importlib.import_module('app.target_planner_coverage').report_coverage(snapshot, plan)


def snapshot(*entries):
    return FilesystemSnapshot('/library', tuple(entries), 255, 4096)


def file(path):
    return FilesystemEntry(path, 'regular', 1, 1)


def directory(path):
    return FilesystemEntry(path, 'directory')


def by_path(rows, path):
    return next(row for row in rows if row.path == path)


def mixed_case():
    ctx = context()
    ctx = replace(ctx,
        collections=(replace(ctx.collections[0], locators=('Show',)),),
        videos=(replace(ctx.videos[0], source='Show/source.mkv'),),
        subtitles=(TargetSubtitle(5, 'Show/source.ass', ((100, 'confirmed_compatible'),)),))
    snap = snapshot(directory('Show'), file('Show/source.mkv'), file('Show/source.ass'),
        file('Show/subs.zip'), directory('Show/Extras'), directory('Show/Extras/Scans'),
        file('Show/Extras/Scans/cover.png'), file('seznam-souboru.txt'))
    return ctx, snap, plan_library(ctx, snap)


def test_complete_mixed_plan_accounts_for_files_and_missing_target_parents():
    _, snap, plan = mixed_case()
    result = coverage(snap, plan)
    assert result.complete and result.files_complete and result.inventory_complete
    assert result.scope == 'PRE_REORGANIZATION' and result.execution_verified is False
    assert result.issues == ()
    assert dict(result.counts)['active_regular_files'] == 5
    assert dict(result.counts)['MOVE'] == 3 and dict(result.counts)['KEEP'] == 2
    assert {row.path for row in result.files} == {
        'Show/source.mkv', 'Show/source.ass', 'Show/subs.zip',
        'Show/Extras/Scans/cover.png', 'seznam-souboru.txt'}
    assert all(row.record_count == 1 for row in result.files)
    assert by_path(result.files, 'Show/subs.zip').object_kind == 'subtitle_archive'
    assert by_path(result.files, 'Show/Extras/Scans/cover.png').object_kind == 'auxiliary'
    assert by_path(result.directories, 'Show/Extras/Scans').disposition == 'PRESERVED'
    missing = {row.path for row in result.directories if row.disposition == 'MISSING_TARGET_PARENT'}
    assert missing == {'Show/Season 01', 'Subs'}
    assert all(not by_path(result.directories, path).exists for path in missing)
    assert all(not by_path(result.directories, path).contents_inventoried for path in missing)


def test_unknown_file_is_review_and_cannot_be_disguised_as_keep():
    ctx = context()
    snap = snapshot(directory('old'), file('old/source.mkv'), file('mystery.mkv'))
    plan = plan_library(ctx, snap)
    result = coverage(snap, plan)
    unknown = by_path(result.files, 'mystery.mkv')
    assert unknown.disposition == 'REVIEW' and unknown.record_count == 1
    assert not result.complete and not result.files_complete
    forged = replace(plan, records=tuple(replace(row, action='KEEP', status='READY',
        target_relative_path=row.source_relative_path, authority=('invented_keep',))
        if row.object_kind == 'unknown' else row for row in plan.records))
    assert by_path(coverage(snap, forged).files, 'mystery.mkv').disposition == 'REVIEW'


def test_uncovered_and_duplicate_sources_are_reported_without_rewriting_records():
    _, snap, plan = mixed_case()
    original = asdict(plan)
    omitted = replace(plan, records=tuple(row for row in plan.records if row.object_kind != 'subtitle'))
    result = coverage(snap, omitted)
    assert by_path(result.files, 'Show/source.ass').record_count == 0
    assert any(issue.code == 'uncovered_file' and issue.path == 'Show/source.ass' for issue in result.issues)
    duplicated = replace(plan, records=plan.records + (next(row for row in plan.records if row.object_kind == 'video'),))
    result = coverage(snap, duplicated)
    assert by_path(result.files, 'Show/source.mkv').record_count == 2
    assert any(issue.code == 'duplicate_plan_source' for issue in result.issues)
    assert not result.complete and not result.files_complete
    assert asdict(plan) == original


def test_empty_nested_directories_and_old_source_directories_require_review():
    ctx = context()
    snap = snapshot(directory('old'), file('old/source.mkv'), directory('old/empty'),
        directory('old/empty/nested'), directory('unassigned'))
    result = coverage(snap, plan_library(ctx, snap))
    assert result.files_complete and not result.complete
    old = by_path(result.directories, 'old')
    assert old.disposition == 'CLEANUP_REVIEW' and old.empty is False
    assert by_path(result.directories, 'old/empty/nested').disposition == 'UNRESOLVED_EMPTY'
    assert by_path(result.directories, 'unassigned').empty is True
    assert by_path(result.directories, 'old/empty').disposition == 'REVIEW'
    assert any(issue.code == 'empty_directory_disposition_unresolved' for issue in result.issues)


def test_root_level_files_can_have_a_complete_plan_with_only_missing_parents():
    ctx = context()
    ctx = replace(ctx, videos=(replace(ctx.videos[0], source='source.mkv'),))
    snap = snapshot(file('source.mkv'))
    result = coverage(snap, plan_library(ctx, snap))
    assert result.complete
    assert {row.path for row in result.directories} == {'Show', 'Show/Season 01'}
    assert all(row.disposition == 'MISSING_TARGET_PARENT' for row in result.directories)


@pytest.mark.parametrize(('ancestor', 'kind'), [
    ('Show/Season 01', 'inaccessible'), ('Show/Season 01', 'symlink'), ('.', 'inaccessible'),
])
def test_target_parent_below_uninspectable_ancestor_has_unknown_existence(ancestor, kind):
    ctx = context(kind='bonus', content='bonus', layout='extras_bonus', ordinal=1)
    snap = snapshot(directory('old'), file('old/source.mkv'),
        FilesystemEntry(ancestor, kind, diagnostic='denied'))
    if ancestor != '.' and kind == 'inaccessible':
        snap = replace(snap, entries=snap.entries + (directory(ancestor),))
    result = coverage(snap, plan_library(ctx, snap))
    parent = by_path(result.directories, 'Show/Season 01/Extras/Bonus')
    assert parent.exists is None
    assert parent.disposition == 'REVIEW' and parent.required_in_target
    assert parent.empty is None and parent.contents_inventoried is False
    assert not result.inventory_complete and not result.complete
    if ancestor != '.' and kind == 'inaccessible':
        assert by_path(result.directories, ancestor).exists is True


def test_uninspectable_sibling_does_not_make_missing_target_parent_unknown():
    ctx, snap, _ = mixed_case()
    snap = replace(snap, entries=snap.entries + (
        directory('Show/locked'), FilesystemEntry('Show/locked', 'inaccessible', diagnostic='listing_error:13')))
    result = coverage(snap, plan_library(ctx, snap))
    parent = by_path(result.directories, 'Show/Season 01')
    assert parent.exists is False and parent.disposition == 'MISSING_TARGET_PARENT'
    assert not result.complete


@pytest.mark.parametrize('kind', ['symlink', 'nonregular', 'inaccessible'])
def test_unsupported_or_uninspectable_entries_are_not_counted_as_known_regular_files(kind):
    ctx, snap, _ = mixed_case()
    snap = replace(snap, entries=snap.entries + (FilesystemEntry('Show/opaque', kind, diagnostic='denied'),))
    result = coverage(snap, plan_library(ctx, snap))
    assert not result.complete and not result.inventory_complete
    assert dict(result.counts)['active_regular_files'] == 5
    assert by_path(result.files, 'Show/opaque').disposition == 'REVIEW'
    if kind == 'inaccessible':
        assert by_path(result.directories, 'Show/opaque').empty is None
        assert by_path(result.directories, 'Show/opaque').exists is None
        assert by_path(result.directories, 'Show').empty is None


def test_recycle_is_separate_excluded_evidence_and_its_ancestor_is_preserved(tmp_path, monkeypatch):
    root = tmp_path / 'library'
    (root / 'Container' / '#recycle').mkdir(parents=True)
    (root / 'Container' / '#recycle' / 'hidden.mkv').write_bytes(b'excluded')
    real = os.scandir
    def no_recycle_descent(path):
        assert str(path).split('/')[-1].casefold() != '#recycle'
        return real(path)
    monkeypatch.setattr(os, 'scandir', no_recycle_descent)
    snap = inventory_filesystem(root)
    ctx = replace(context(), videos=(), collections=(), titles=())
    result = coverage(snap, plan_library(ctx, snap))
    assert result.complete and result.files == ()
    excluded = by_path(result.directories, 'Container/#recycle')
    assert excluded.disposition == 'EXCLUDED' and excluded.contents_inventoried is False
    assert excluded.empty is None
    assert by_path(result.directories, 'Container').disposition == 'PRESERVED'
    assert dict(result.counts)['excluded_areas'] == 1
    assert dict(result.counts)['active_regular_files'] == 0


@pytest.mark.parametrize('locked_path', ['locked', '.'])
def test_actual_unreadable_subtree_is_reported_without_claiming_it_is_a_file_or_empty(tmp_path, monkeypatch, locked_path):
    root = tmp_path / 'library'
    locked_directory = root / locked_path
    locked_directory.mkdir(parents=True)
    real = os.scandir
    def deny_locked(path):
        if path == locked_directory:
            raise PermissionError(13, 'synthetic denied')
        return real(path)
    monkeypatch.setattr(os, 'scandir', deny_locked)
    snap = inventory_filesystem(root)
    ctx = replace(context(), videos=(), collections=(), titles=())
    plan = plan_library(ctx, snap)
    result = coverage(snap, plan)
    assert not result.inventory_complete and not result.complete
    locked = by_path(result.directories, locked_path)
    assert locked.disposition == 'REVIEW' and locked.empty is None
    assert locked.contents_inventoried is False
    assert any(issue.code == 'inaccessible_inventory' and issue.path == locked_path for issue in result.issues)
    assert result.files == ()
    assert dict(result.counts)['file_entries'] == dict(result.counts)['REVIEW'] == 0
    assert dict(result.counts)['active_regular_files'] == 0
    assert len(plan.records) == 1 and plan.records[0].action == 'REVIEW'


def test_collision_blocks_completeness_even_when_every_source_is_represented():
    ctx, snap, plan = mixed_case()
    snap = replace(snap, entries=snap.entries + (directory('show'),))
    plan = plan_library(ctx, snap)
    assert plan.collisions
    result = coverage(snap, plan)
    assert not result.complete
    assert any(issue.code.startswith('target_collision:') for issue in result.issues)
    assert all(row.record_count == 1 for row in result.files)


def test_missing_db_source_and_snapshot_diagnostics_cannot_claim_completeness():
    ctx, snap, _ = mixed_case()
    snap = replace(snap, entries=tuple(e for e in snap.entries if e.relative_path != 'Show/source.mkv'),
        diagnostics=('pc_name_max_not_checked',))
    result = coverage(snap, plan_library(ctx, snap))
    assert not result.complete and not result.inventory_complete
    missing = by_path(result.files, 'Show/source.mkv')
    assert missing.inventory_kind == 'not_in_inventory' and missing.disposition == 'REVIEW'
    assert any(issue.code == 'plan_source_not_in_active_inventory' for issue in result.issues)


def test_duplicate_inventory_path_is_uncertainty_not_two_accounted_files():
    ctx, snap, _ = mixed_case()
    snap = replace(snap, entries=snap.entries + (file('Show/source.mkv'),))
    result = coverage(snap, plan_library(ctx, snap))
    assert not result.inventory_complete and not result.complete
    assert dict(result.counts)['active_regular_files'] == 5
    assert any(issue.code == 'duplicate_inventory_path' for issue in result.issues)


def test_existing_non_directory_target_parent_cannot_be_reported_as_missing_or_ready():
    ctx, snap, _ = mixed_case()
    snap = replace(snap, entries=snap.entries + (file('Show/Season 01'),))
    result = coverage(snap, plan_library(ctx, snap))
    parent = by_path(result.directories, 'Show/Season 01')
    assert parent.disposition == 'REVIEW' and parent.empty is None
    assert any(issue.code == 'target_parent_not_directory' for issue in result.issues)
    assert not result.complete


def test_keep_without_authority_does_not_justify_preserving_its_parents():
    _, snap, plan = mixed_case()
    plan = replace(plan, records=tuple(replace(row, authority=())
        if row.source_relative_path == 'Show/Extras/Scans/cover.png' else row for row in plan.records))
    result = coverage(snap, plan)
    assert by_path(result.files, 'Show/Extras/Scans/cover.png').disposition == 'REVIEW'
    assert not result.complete


def test_nonempty_directory_is_not_called_empty_when_only_a_deeper_child_was_observed():
    ctx, snap, _ = mixed_case()
    snap = replace(snap, entries=snap.entries + (directory('abandoned'), file('abandoned/deep/unknown.mkv')))
    result = coverage(snap, plan_library(ctx, snap))
    abandoned = by_path(result.directories, 'abandoned')
    assert abandoned.empty is False and abandoned.disposition == 'REVIEW'


@pytest.mark.parametrize(('duplicate_kind', 'duplicate_id'), [('subtitle', 99), ('video', '100')])
def test_distinct_records_with_the_same_source_have_stable_diagnostic_order(duplicate_kind, duplicate_id):
    _, snap, plan = mixed_case()
    original = next(row for row in plan.records if row.object_kind == 'video')
    duplicate = replace(original, object_kind=duplicate_kind, object_id=duplicate_id)
    plan = replace(plan, records=plan.records + (duplicate,))
    result = coverage(snap, plan)
    other = coverage(snap, replace(plan, records=tuple(reversed(plan.records))))
    assert result == other and not result.complete
    assert by_path(result.files, 'Show/source.mkv').record_count == 2


def test_quarantine_and_confirmed_no_match_remain_distinct_file_dispositions():
    from app.target_planner_types import UnmatchedSubtitle
    ctx = context()
    secondary = replace(ctx.videos[0], id=101, source='old/copy.mkv',
        duplicate_primary_id=100, duplicate_validity='valid')
    ctx = replace(ctx, videos=ctx.videos + (secondary,),
        unmatched=(UnmatchedSubtitle(6, 'old/orphan.ass', 'confirmed_no_match'),))
    snap = snapshot(directory('old'), file('old/source.mkv'), file('old/copy.mkv'), file('old/orphan.ass'))
    result = coverage(snap, plan_library(ctx, snap))
    assert by_path(result.files, 'old/copy.mkv').disposition == 'QUARANTINE'
    assert by_path(result.files, 'old/orphan.ass').disposition == 'MOVE'
    assert result.files_complete and not result.complete  # old needs cleanup review


def test_report_is_deterministic_and_uses_only_supplied_evidence(monkeypatch):
    import builtins
    import io
    import sqlite3
    _, snap, plan = mixed_case()
    report = importlib.import_module('app.target_planner_coverage').report_coverage
    def forbidden(*args, **kwargs):
        pytest.fail('coverage must not access or mutate the filesystem')
    with monkeypatch.context() as guard:
        for name in ('scandir', 'stat', 'lstat', 'open', 'mkdir', 'rename', 'rmdir', 'unlink'):
            guard.setattr(os, name, forbidden)
        for module, name in ((builtins, 'open'), (io, 'open'), (sqlite3, 'connect')):
            guard.setattr(module, name, forbidden)
        result = report(snap, plan)
        reversed_result = report(replace(snap, entries=tuple(reversed(snap.entries))),
            replace(plan, records=tuple(reversed(plan.records))))
    assert result == reversed_result
    assert json.dumps(asdict(result), sort_keys=True) == json.dumps(asdict(reversed_result), sort_keys=True)


@pytest.mark.parametrize('root_level', [False, True])
def test_cli_distinguishes_directory_review_from_complete_coverage_even_with_converged_projection(tmp_path, session, root_level):
    from app.tools.target_plan import exit_status, simulate_readonly
    from app.models import ExternalSubtitle
    from test_target_planner_service import seed
    from sqlalchemy import select
    import sqlite3
    _, _, videos = seed(session, count=1)
    if root_level:
        videos[0].relative_path = 'video1.mkv'
        videos[0].root_folder = '.'
        session.scalar(select(ExternalSubtitle)).relative_path = 'video1.ass'
        session.commit()
    path = tmp_path / 'fixture.db'
    with sqlite3.connect(path) as target:
        session.connection().connection.driver_connection.backup(target)
    root = tmp_path / 'library'
    source = root / videos[0].relative_path
    source.parent.mkdir(parents=True)
    source.write_bytes(b'video')
    source.with_suffix('.ass').write_bytes(b'subtitle')
    result = simulate_readonly(path, root, repeat=2, post_state=True)
    assert result['coverage_deterministic']
    assert result['coverage']['files_complete']
    assert result['coverage']['complete'] == root_level
    assert result['coverage']['execution_verified'] is False
    assert result['post_state']['converged'] is True
    assert exit_status(result) == (0 if root_level else 2)
    assert result['production_db_unchanged'] and result['nas_write_operations'] == 0
    assert result['sql_select_counts'] == [11, 11]


def test_cli_exit_status_never_calls_a_report_ready_when_coverage_is_missing_or_incomplete():
    from app.tools.target_plan import exit_status
    base = dict(deterministic=True, production_db_unchanged=True, coverage_deterministic=True,
        plan=dict(status_counts=[('BLOCKED', 0), ('REVIEW', 0)]))
    assert exit_status(base) != 0
    assert exit_status(dict(base, coverage={'complete': False})) == 2
    assert exit_status(dict(base, coverage={'complete': True})) == 0
    assert exit_status(dict(base, coverage={'complete': True}, coverage_deterministic=False)) == 1
    assert exit_status(dict(base, coverage={'complete': False}, post_state={'converged': True},
        post_state_deterministic=False)) == 1
