"""Read-only post-state closure keeps IDs, controlled locators and preservation lanes."""
from dataclasses import FrozenInstanceError, replace
import importlib
import importlib.util
from pathlib import PurePosixPath

import pytest

from app.target_planner import project_targets, source_collection
from app.target_planner_filesystem import inventory_filesystem, plan_library
from app.target_planner_types import FilesystemEntry, SideAssetEvidence, TargetSubtitle, UnmatchedSubtitle
from test_target_planner import context
from test_target_planner_filesystem import snapshot, by_source


def projection_api():
    assert importlib.util.find_spec('app.target_planner_post_state') is not None, 'Pure post-state projection missing'
    return importlib.import_module('app.target_planner_post_state')


def locators(ctx):
    domain = importlib.import_module('app.target_planner')
    assert hasattr(domain, 'project_container_locators'), 'Explicit container locator projection missing'
    return domain.project_container_locators(ctx)


def title_locator(ctx, id=10):
    return next(r for r in locators(ctx) if r.object_kind == 'title' and r.object_id == id)


def mixed_context(*, season=1, layout='extras_promo', second_content='preview'):
    ctx = context(kind='bonus', season=season, content='cm', layout=layout, ordinal=1)
    layout_titles = dict(ctx.layout.titles)
    layout_titles[10] = replace(layout_titles[10], content_types=('cm', second_content), logical_count=2)
    layout_ctx = replace(ctx.layout, titles=layout_titles)
    from app.physical_layout import create_basis_snapshot
    layout_ctx = replace(layout_ctx, choices={10: replace(layout_ctx.choices[10], basis_snapshot_json=create_basis_snapshot(layout_ctx, 10))})
    return replace(ctx, layout=layout_ctx, videos=ctx.videos+(replace(ctx.videos[0], id=101, source='old/pv.mkv', content_type=second_content),))


@pytest.mark.parametrize('extension', ['zip', 'RAR', '7z'])
def test_flat_subs_archives_are_ownerless_ready_keep(extension):
    source = f'Subs/Original.{extension}'
    result = plan_library(replace(context(), videos=()), snapshot(source))
    row = by_source(result, source)
    assert row.object_kind == 'subtitle_archive'
    assert row.action == 'KEEP' and row.status == 'READY'
    assert row.target_relative_path == source
    assert row.collection_id is None and row.title_id is None


@pytest.mark.parametrize('source', ['Subs/readme.txt', 'Subs/nested/sub.zip', 'subs/sub.zip'])
def test_unexpected_subs_objects_are_review(source):
    row = by_source(plan_library(replace(context(), videos=()), snapshot(source)), source)
    assert row.status == 'REVIEW' and row.target_relative_path is None


def test_reserved_roots_cannot_be_collection_evidence():
    ctx = replace(context(), collections=(replace(context().collections[0], locators=('Subs', 'Duplicates', '#recycle')),))
    assert all(source_collection(ctx, p) is None for p in ('Subs/a.zip', 'Duplicates/a.flac', '#recycle/a.mkv'))
    assert by_source(plan_library(ctx, snapshot('old/source.mkv', 'Duplicates/a.flac')), 'Duplicates/a.flac').status == 'REVIEW'


def test_unprovenanced_duplicates_archive_does_not_become_source_archive():
    source = 'Duplicates/Show/Season 01/subs.zip'
    ctx = replace(context(), videos=(), collections=(replace(context().collections[0], locators=('Duplicates',)),))
    row = by_source(plan_library(ctx, snapshot(source)), source)
    assert row.status == 'REVIEW' and row.duplicate is None
    assert row.target_relative_path is None


def test_recycle_has_context_count_but_no_plan_record(tmp_path, monkeypatch):
    (tmp_path/'#recycle').mkdir()
    (tmp_path/'#recycle/hidden.zip').write_bytes(b'not inventory')
    import app.target_planner_filesystem as fs
    real = fs.os.scandir
    def guarded(path):
        assert PurePosixPath(path).name != '#recycle'
        return real(path)
    monkeypatch.setattr(fs.os, 'scandir', guarded)
    result = plan_library(replace(context(), videos=()), inventory_filesystem(tmp_path))
    assert result.records == ()
    assert dict(result.counts)['system_excluded_dirs'] == 1
    assert dict(result.counts)['filesystem_visible_assets'] == 0


def test_subs_archive_recognition_does_not_require_content_reads(tmp_path, monkeypatch):
    (tmp_path/'Subs').mkdir()
    (tmp_path/'Subs/a.zip').write_bytes(b'not parsed')
    import app.target_planner_filesystem as fs
    monkeypatch.setattr(fs.os, 'open', lambda *a, **k: pytest.fail('Subs archives need no content read'))
    result = plan_library(replace(context(), videos=()), inventory_filesystem(tmp_path))
    assert by_source(result, 'Subs/a.zip').action == 'KEEP'


@pytest.mark.parametrize('ctx,want', [
    (context(), 'Show/Season 01'),
    (context(kind='bonus', content='cm', layout='extras_promo'), 'Show/Season 01/Extras/CM'),
    (context(kind='bonus', content='preview', layout='extras_promo'), 'Show/Season 01/Extras/Promo'),
    (mixed_context(), 'Show/Season 01/Extras'),
    (mixed_context(season=None), 'Show/Extras'),
    (mixed_context(layout='own_folder'), 'Show/Season 01/Show'),
])
def test_explicit_container_locator_contract(ctx, want):
    row = title_locator(ctx)
    assert row.target_relative_path == want and row.status == 'READY'
    assert row.object_id == ctx.titles[0].id
    assert {r.title_id for r in project_targets(ctx)} == {10}
    assert all(PurePosixPath(r.target_relative_path).is_relative_to(want) for r in project_targets(ctx))
    with pytest.raises(FrozenInstanceError):
        row.target_relative_path = 'other'


def test_mixed_leaf_targets_are_still_cm_and_promo():
    ctx = mixed_context()
    assert title_locator(ctx).target_relative_path == 'Show/Season 01/Extras'
    assert {str(PurePosixPath(r.target_relative_path).parent) for r in project_targets(ctx)} == {'Show/Season 01/Extras/CM','Show/Season 01/Extras/Promo'}


def test_unapproved_fanout_is_review_instead_of_lca():
    row = title_locator(mixed_context(second_content='bonus'))
    assert row.status == 'REVIEW' and row.target_relative_path is None
    assert 'unapproved_container_fanout' in row.blockers


def archive_context():
    ctx = context(season=2)
    copy = replace(ctx.videos[0], id=101, source='old/copy/Original.MKV', duplicate_primary_id=100, duplicate_validity='valid')
    sub = TargetSubtitle(200, 'old/copy/Original.ass', ((101,'confirmed_compatible'),))
    ctx = replace(ctx, videos=ctx.videos+(copy,), subtitles=(sub,))
    entries = snapshot('old/source.mkv', copy.source, sub.source, 'old/subs.zip', 'old/copy/subs.zip', 'old/OST/audio.flac', 'seznam-souboru.txt')
    entries = replace(entries, entries=tuple(replace(e, content_sha256='archive-proof') if e.relative_path.endswith('.zip') else e for e in entries.entries))
    return ctx, entries


def test_pure_post_state_accounts_both_archives_and_settles_every_move():
    api = projection_api()
    ctx, snap = archive_context()
    current = plan_library(ctx, snap)
    result = api.simulate_post_state(ctx, snap, current)
    assert result.converged and result.blockers == ()
    assert dict(result.plan.status_counts)['REVIEW'] == 0
    assert all(r.action == 'KEEP' and r.source_relative_path == r.target_relative_path for r in result.plan.records)
    assert by_source(result.plan, 'Subs/subs.zip').object_kind == 'subtitle_archive'
    zipped = by_source(result.plan, 'Duplicates/Show/Season 02/subs.zip')
    assert zipped.object_kind == 'duplicate_side_asset' and zipped.duplicate.secondary_video_id == 101
    assert zipped.duplicate.purge_state == 'PRE_EXECUTION_REQUIRED'
    assert {v.id for v in result.context.videos} == {100,101}
    assert result.context.subtitles[0].compatibility == ctx.subtitles[0].compatibility
    assert ctx.videos[0].source == 'old/source.mkv' and snap.entries[0].relative_path == 'old/source.mkv'
    second = api.simulate_post_state(result.context, result.snapshot, result.plan)
    assert second.converged and second.plan == result.plan
    assert second.context == result.context and second.locators == result.locators
    assert second.projection_hash == result.projection_hash


def test_post_state_preserves_incomplete_side_asset_accounting():
    api = projection_api()
    ctx, snap = archive_context()
    copy_aux = FilesystemEntry('old/copy/Scan/image.png', 'regular', 2, 2)
    snap = replace(snap, entries=snap.entries+(copy_aux,))
    current = plan_library(ctx, snap)
    assert by_source(current, 'old/copy/Original.MKV').duplicate.side_assets_accounted is False
    result = api.simulate_post_state(ctx, snap, current)
    secondary = next(r for r in result.plan.records if r.object_kind == 'video' and r.object_id == 101)
    assert result.converged and secondary.duplicate.side_assets_accounted is False
    assert secondary.duplicate.purge_state == 'PRE_EXECUTION_REQUIRED'


def test_post_state_rejects_stale_plan_and_blocked_inputs():
    api = projection_api()
    ctx, snap = archive_context()
    current = plan_library(ctx, snap)
    with pytest.raises(ValueError, match='stale'):
        api.simulate_post_state(ctx, replace(snap, entries=snap.entries[:-1]), current)
    changed = replace(ctx, videos=(replace(ctx.videos[0], episode_number=99),)+ctx.videos[1:])
    with pytest.raises(ValueError, match='stale'):
        api.simulate_post_state(changed, snap, current)
    missing = replace(snap, entries=tuple(e for e in snap.entries if e.relative_path != ctx.videos[0].source))
    blocked = plan_library(ctx, missing)
    with pytest.raises(ValueError, match='BLOCKED|REVIEW'):
        api.simulate_post_state(ctx, missing, blocked)


def test_post_state_calls_no_filesystem_sql_or_normal_scanner(monkeypatch):
    api = projection_api()
    ctx, snap = archive_context()
    current = plan_library(ctx, snap)
    import os
    def forbidden(*args, **kwargs):
        pytest.fail('post-state projection must be pure')
    for name in ('scandir','stat','lstat','open','rename','replace','unlink','mkdir'):
        monkeypatch.setattr(os, name, forbidden)
    assert api.simulate_post_state(ctx, snap, current).converged


def test_post_state_safe_continuation_remains_in_season():
    api = projection_api()
    ctx = context(season=2)
    ctx = replace(ctx, videos=tuple(replace(ctx.videos[0], id=100+n, source=f'old/Series - {n:02}.mkv', episode_number=n) for n in (10,11,12)),
        subtitles=tuple(TargetSubtitle(200+n,f'old/Series - {n:02}.ass',((100+n,'confirmed_compatible'),)) for n in (10,11,12)),
        unmatched=(UnmatchedSubtitle(300,'old/Series - 13.ass','confirmed_no_match'),))
    snap = snapshot(*(v.source for v in ctx.videos),*(s.source for s in ctx.subtitles),ctx.unmatched[0].source)
    result = api.simulate_post_state(ctx,snap,plan_library(ctx,snap))
    assert result.converged
    row = next(r for r in result.plan.records if r.object_kind=='confirmed_no_match')
    assert row.action=='KEEP' and row.target_relative_path=='Show/Season 02/Show - S02E13.ass'
    assert row.status=='WARNING' and row.compatibility==()


def test_shared_season_parts_have_shared_locator_and_distinct_ids():
    from app.physical_naming import create_basis_snapshot
    ctx = context(kind='part', season=2, part=1)
    titles = (replace(ctx.titles[0], source_locator='old/shared'), replace(ctx.titles[0], id=11, part=2, source_locator='old/shared'))
    naming_titles = dict(ctx.naming.titles)
    naming_titles[11] = replace(naming_titles[10], id=11, part_number=2)
    names = replace(ctx.naming, collections={1:(10,11)}, titles=naming_titles)
    names = replace(names, choices={('collection',1): replace(names.choices[('collection',1)], basis_snapshot_json=create_basis_snapshot(names,'collection',1))})
    ctx = replace(ctx, naming=names, titles=titles, videos=ctx.videos+(replace(ctx.videos[0], id=101, title_id=11, source='old/part2.mkv'),))
    assert {r.target_relative_path for r in locators(ctx) if r.object_kind=='title'} == {'Show/Season 02'}
    api = projection_api()
    snap = snapshot(*(v.source for v in ctx.videos))
    result = api.simulate_post_state(ctx,snap,plan_library(ctx,snap))
    assert result.converged and {(v.id,v.title_id) for v in result.context.videos} == {(100,10),(101,11)}
    assert {t.source_locator for t in result.context.titles} == {'Show/Season 02'}
    assert [t.part for t in result.context.titles] == [1,2]


def test_post_state_is_deterministic_with_reversed_input_order():
    api = projection_api()
    ctx, snap = archive_context()
    result = api.simulate_post_state(ctx,snap,plan_library(ctx,snap))
    reverse = replace(ctx,videos=tuple(reversed(ctx.videos)),subtitles=tuple(reversed(ctx.subtitles)))
    rev_snap = replace(snap,entries=tuple(reversed(snap.entries)))
    other = api.simulate_post_state(reverse,rev_snap,plan_library(reverse,rev_snap))
    assert other == result


def test_post_state_explicit_side_evidence_mismatch_is_not_accounted():
    ctx, snap = archive_context()
    side = SideAssetEvidence('old/copy/subs.zip',101,'approved_evidence',expected_size=999,expected_sha256='different')
    ctx = replace(ctx,side_assets=(side,))
    result = plan_library(ctx,snap)
    row = by_source(result,side.source)
    assert row.status=='REVIEW' and 'duplicate_side_asset_evidence_mismatch' in row.blockers
    assert by_source(result,ctx.videos[1].source).duplicate.side_assets_accounted is False


@pytest.mark.parametrize('directory',[
    'Show/Season 01/Show - S01E01.mkv',
    'sHOW/sEASON 01/sHOW - s01e01.MKV',
    'ſhow/Season 01',
])
def test_existing_directory_namespace_conflicts_block_projection(directory):
    ctx=context()
    snap=snapshot('old/source.mkv')
    snap=replace(snap,entries=snap.entries+(FilesystemEntry(directory,'directory'),))
    plan=plan_library(ctx,snap)
    assert plan.collisions and by_source(plan,'old/source.mkv').status=='BLOCKED'
    with pytest.raises(ValueError,match='BLOCKED|REVIEW'):
        projection_api().simulate_post_state(ctx,snap,plan)


def test_matching_existing_parent_directory_is_allowed():
    ctx=context()
    snap=snapshot('old/source.mkv')
    snap=replace(snap,entries=snap.entries+(FilesystemEntry('Show/Season 01','directory'),))
    plan=plan_library(ctx,snap)
    assert plan.collisions==() and projection_api().simulate_post_state(ctx,snap,plan).converged


def test_subs_side_evidence_conflict_does_not_override_reserved_lane():
    ctx,snap=archive_context()
    source='Subs/wrong-lane.zip'
    ctx=replace(ctx,side_assets=(SideAssetEvidence(source,101,'explicit_secondary'),))
    snap=replace(snap,entries=snap.entries+(FilesystemEntry(source,'regular',1,1),))
    row=by_source(plan_library(ctx,snap),source)
    assert row.status=='REVIEW' and row.target_relative_path is None
    assert 'side_asset_evidence_conflicts_with_subs_lane' in row.blockers


def test_empty_side_provenance_cannot_account_for_a_quarantine_file():
    ctx,snap=archive_context()
    ctx=replace(ctx,side_assets=(SideAssetEvidence('old/copy/subs.zip',101,''),))
    row=by_source(plan_library(ctx,snap),'old/copy/subs.zip')
    assert row.status=='REVIEW' and row.target_relative_path is None


@pytest.mark.parametrize('kind', ['video', 'subtitle', 'unmatched', 'side_asset'])
@pytest.mark.parametrize('has_snapshot', [True, False])
def test_excluded_db_asset_is_missing_from_active_library_without_traversal(kind, has_snapshot):
    api=projection_api()
    ctx=replace(context(), videos=())
    source='#recycle/old/source.mkv'
    if kind == 'video':
        ctx=replace(ctx, videos=(replace(context().videos[0], source=source),))
    elif kind == 'subtitle':
        ctx=replace(ctx, subtitles=(TargetSubtitle(200, source, ()),))
    elif kind == 'unmatched':
        ctx=replace(ctx, unmatched=(UnmatchedSubtitle(300, source, 'confirmed_no_match'),))
    else:
        ctx=replace(ctx, side_assets=(SideAssetEvidence(source, 101, 'explicit_secondary'),))
    snap=replace(snapshot(),entries=(FilesystemEntry('#recycle','system_excluded'),))
    plan=plan_library(ctx,snap if has_snapshot else None)
    # #recycle is deleted/outside the active library: the DB row stays visible
    # with the existing missing-file state, no recovery target, no review of bin.
    row, = plan.records
    assert row.source_relative_path == source and row.status == 'BLOCKED'
    assert row.target_relative_path is None and row.action == 'REVIEW'
    assert 'source_missing_or_not_regular' in row.blockers
    assert 'DB_ASSET_IN_SYSTEM_EXCLUDED_ROOT' not in row.blockers
    if has_snapshot:
        with pytest.raises(ValueError,match='BLOCKED|REVIEW'):
            api.simulate_post_state(ctx,snap,plan)


def test_unknown_accounting_remains_unknown_through_permitted_quarantine_projection():
    ctx=context()
    copy=replace(ctx.videos[0],id=101,source='Duplicates/Show/Season 01/copy.mkv',
                 duplicate_primary_id=100,duplicate_validity='valid')
    ctx=replace(ctx,videos=ctx.videos+(copy,))
    snap=snapshot(ctx.videos[0].source,copy.source)
    plan=plan_library(ctx,snap)
    assert by_source(plan,copy.source).duplicate.side_asset_accounting == 'UNKNOWN'
    result=projection_api().simulate_post_state(ctx,snap,plan)
    assert result.converged
    assert by_source(result.plan,copy.source).duplicate.side_asset_accounting == 'UNKNOWN'
    assert not by_source(result.plan,copy.source).duplicate.side_assets_accounted


def test_post_state_api_requires_explicit_evidence_even_for_a_pure_context():
    ctx,snap=archive_context()
    api=projection_api()
    projected=api.simulate_post_state(ctx,snap,plan_library(ctx,snap))
    result=api.verify_post_state(projected.context,projected.snapshot)
    assert by_source(result,'Duplicates/Show/Season 02/Original.MKV').duplicate.side_asset_accounting == 'UNKNOWN'
    assert by_source(result,'Duplicates/Show/Season 02/subs.zip').status == 'REVIEW'


def test_carried_archive_hash_evidence_survives_second_projection_without_subs_rehash():
    ctx=context()
    copy=replace(ctx.videos[0],id=101,source='old/copy.mkv',duplicate_primary_id=100,duplicate_validity='valid')
    ctx=replace(ctx,videos=ctx.videos+(copy,))
    snap=snapshot('old/source.mkv',copy.source,'old/unrelated.zip')
    snap=replace(snap,entries=tuple(replace(e,content_sha256='original-archive-hash')
        if e.relative_path.endswith('.zip') else e for e in snap.entries))
    api=projection_api()
    result=api.simulate_post_state(ctx,snap,plan_library(ctx,snap))
    assert result.converged
    assert by_source(result.plan,'Subs/unrelated.zip').object_kind == 'subtitle_archive'
    assert next(e for e in result.snapshot.entries if e.relative_path == 'Subs/unrelated.zip').content_sha256 is None
    second=api.simulate_post_state(result.context,result.snapshot,result.plan)
    assert second == result


def test_carried_db_subtitle_stat_mismatch_is_review_and_incomplete():
    ctx,snap=archive_context()
    api=projection_api()
    p=api.simulate_post_state(ctx,snap,plan_library(ctx,snap))
    changed=replace(p.snapshot,entries=tuple(replace(e,size=999) if e.relative_path.endswith('Original.ass') else e for e in p.snapshot.entries))
    result=api.verify_post_state(p.context,changed,execution_evidence=p.execution_evidence)
    assert by_source(result,'Duplicates/Show/Season 02/Original.ass').status == 'REVIEW'
    assert by_source(result,'Duplicates/Show/Season 02/Original.MKV').duplicate.side_asset_accounting == 'INCOMPLETE'


def test_carried_db_subtitle_without_classification_provenance_is_review():
    ctx,snap=archive_context()
    api=projection_api()
    p=api.simulate_post_state(ctx,snap,plan_library(ctx,snap))
    evidence=tuple(replace(e,known_side_assets=tuple(replace(s,provenance='')
        if s.object_kind == 'subtitle' else s for s in e.known_side_assets)) for e in p.execution_evidence)
    result=api.verify_post_state(p.context,p.snapshot,execution_evidence=evidence)
    assert by_source(result,'Duplicates/Show/Season 02/Original.ass').status == 'REVIEW'
    assert by_source(result,'Duplicates/Show/Season 02/Original.MKV').duplicate.side_asset_accounting == 'INCOMPLETE'


@pytest.mark.parametrize('change', ['path', 'hash', 'missing_primary', 'changed_primary_size'])
def test_carried_archive_copy_proof_must_match_primary_identity_without_rehash(change):
    ctx,snap=archive_context()
    api=projection_api()
    p=api.simulate_post_state(ctx,snap,plan_library(ctx,snap))
    evidence=p.execution_evidence
    snapshot=p.snapshot
    if change in {'path','hash'}:
        evidence=tuple(replace(e,known_side_assets=tuple(replace(s,
            **({'primary_archive_source':'Fake/nothing.zip'} if change == 'path' else {'primary_archive_sha256':'wrong-primary-hash'}))
            if s.object_kind == 'duplicate_side_asset' else s for s in e.known_side_assets)) for e in evidence)
    elif change == 'missing_primary':
        snapshot=replace(snapshot,entries=tuple(e for e in snapshot.entries if e.relative_path != 'Subs/subs.zip'))
    else:
        snapshot=replace(snapshot,entries=tuple(replace(e,size=999) if e.relative_path == 'Subs/subs.zip' else e for e in snapshot.entries))
    result=api.verify_post_state(p.context,snapshot,execution_evidence=evidence)
    assert by_source(result,'Duplicates/Show/Season 02/subs.zip').status == 'REVIEW'
    assert by_source(result,'Duplicates/Show/Season 02/Original.MKV').duplicate.side_asset_accounting == 'INCOMPLETE'


@pytest.mark.parametrize('extra', [(), ('old/Scan/cover.png',)])
def test_carried_evidence_lists_only_assets_owned_by_that_secondary(extra):
    # Directory neighbours (primary sidecar, auxiliary) are not side assets of
    # the secondary; a future purge must never inherit them from evidence.
    ctx=context()
    copy=replace(ctx.videos[0],id=101,source='old/copy.mkv',duplicate_primary_id=100,duplicate_validity='valid')
    subs=(TargetSubtitle(200,'old/source.ass',((100,'automatic_match'),)),
          TargetSubtitle(201,'old/copy.ass',((101,'confirmed_compatible'),)))
    ctx=replace(ctx,videos=ctx.videos+(copy,),subtitles=subs)
    snap=snapshot('old/source.mkv',copy.source,*(s.source for s in subs),*extra)
    result=projection_api().simulate_post_state(ctx,snap,plan_library(ctx,snap))
    evidence,=result.execution_evidence
    assert evidence.side_asset_accounting==('INCOMPLETE' if extra else 'COMPLETE')
    assert [(s.object_kind,s.object_id,s.source) for s in evidence.known_side_assets]==[
        ('subtitle',201,'Duplicates/Show/Season 01/copy.ass')]
    assert result.converged


def test_recycle_db_row_is_kept_without_ever_entering_recycle(tmp_path, monkeypatch):
    (tmp_path/'#recycle/old').mkdir(parents=True)
    (tmp_path/'#recycle/old/source.mkv').write_bytes(b'deleted from the active library')
    import app.target_planner_filesystem as fs
    real = fs.os.scandir
    def guarded(path):
        assert '#recycle' not in PurePosixPath(path).parts, 'entered #recycle'
        return real(path)
    monkeypatch.setattr(fs.os, 'scandir', guarded)
    ctx = replace(context(), videos=(replace(context().videos[0], source='#recycle/old/source.mkv'),))
    plan = plan_library(ctx, inventory_filesystem(tmp_path))
    row, = plan.records
    assert (row.object_kind, row.object_id, row.status, row.target_relative_path) == ('video', 100, 'BLOCKED', None)
    assert row.blockers == ('source_missing_or_not_regular',)
    assert dict(plan.counts)['filesystem_visible_assets'] == 0 and dict(plan.counts)['system_excluded_dirs'] == 1
