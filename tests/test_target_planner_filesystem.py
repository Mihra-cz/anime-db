"""One snapshot, complete coverage, conservative namespace and safety gates."""
from dataclasses import replace
import importlib
import importlib.util
from pathlib import Path

import pytest

from test_target_planner import api as domain_api, context


def api():
    assert importlib.util.find_spec('app.target_planner_filesystem') is not None, 'Planner inventory/preflight missing'
    return importlib.import_module('app.target_planner_filesystem'), domain_api()[1]


def snapshot(*paths, name_max=255, path_max=4096):
    _, t = api()
    return t.FilesystemSnapshot('/library', tuple(t.FilesystemEntry(p, 'regular', 1, 1) for p in paths), name_max, path_max)


def plan(ctx, snap):
    f, _ = api()
    return f.plan_library(ctx, snap)


def by_source(result, source):
    return next(r for r in result.records if r.source_relative_path == source)


def test_archive_is_flat_original_and_root_txt_is_keep():
    result = plan(context(), snapshot('old/source.mkv', 'old/nested/Komplet-5.zip', 'seznam-souboru.txt'))
    archive = by_source(result, 'old/nested/Komplet-5.zip')
    assert archive.object_kind == 'subtitle_archive' and archive.target_relative_path == 'Subs/Komplet-5.zip'
    # Approved mechanical routing: uninspected contents are information only.
    assert archive.status == 'READY' and archive.warnings == ()
    assert archive.info == ('archive_source_preserved_contents_not_classified',)
    txt = by_source(result, 'seznam-souboru.txt')
    assert txt.action == 'KEEP' and txt.target_relative_path == 'seznam-souboru.txt'
    assert dict(result.counts)['filesystem_visible_assets'] == 3


@pytest.mark.parametrize('subtree', ['Audio CDs/OST', 'Audio Guide', 'Audio 5.1', 'BD menu', 'Scan', 'OST/ED', 'Unusual bundle/Disc 1'])
def test_auxiliary_preserves_actual_subtree_and_filename(subtree):
    ctx = context()
    source = f'old/Season old/{subtree}/Original File.flac'
    ctx = replace(ctx, titles=(replace(ctx.titles[0], source_locator='old/Season old'),))
    result = plan(ctx, snapshot('old/source.mkv', source))
    aux = by_source(result, source)
    assert aux.object_kind == 'auxiliary'
    assert aux.target_relative_path == f'Show/Extras/{subtree}/Original File.flac'


@pytest.mark.parametrize('extras', ['Extras', 'extras', 'EXTRAS'])
def test_existing_extras_is_not_duplicated(extras):
    source = f'old/{extras}/OST/S1/song.flac'
    result = plan(context(), snapshot('old/source.mkv', source))
    assert by_source(result, source).target_relative_path == 'Show/Extras/OST/S1/song.flac'


def test_explicit_secondary_zip_uses_quarantine_and_preserves_filename():
    _, t = api()
    ctx = context(season=2)
    secondary = replace(ctx.videos[0], id=101, source='old/copy/Uzaki old.mkv', duplicate_primary_id=100, duplicate_validity='valid')
    source = 'old/copy/Uzaki original ZIP.zip'
    ctx = replace(ctx, videos=ctx.videos+(secondary,), side_assets=(t.SideAssetEvidence(source, 101, 'explicit_human_secondary_archive'),))
    result = plan(ctx, snapshot('old/source.mkv', secondary.source, source))
    row = by_source(result, source)
    assert row.target_relative_path == 'Duplicates/Show/Season 02/Uzaki original ZIP.zip'
    assert row.action == 'QUARANTINE'
    assert row.duplicate.primary_video_id == 100
    assert source in by_source(result, secondary.source).duplicate.side_assets


def test_archive_duplicate_requires_corrobated_copy_not_just_directory_name():
    _, t = api()
    ctx = context()
    secondary = replace(ctx.videos[0], id=101, source='old/copy/copy.mkv', duplicate_primary_id=100, duplicate_validity='valid')
    ctx = replace(ctx, videos=ctx.videos+(secondary,))
    src = 'old/copy/subs.zip'
    entries = snapshot(ctx.videos[0].source, secondary.source, src, 'old/subs.zip')
    result = plan(ctx, entries)
    assert by_source(result, src).object_kind == 'duplicate_quarantine_candidate'
    assert by_source(result, src).status == 'REVIEW'
    entries = replace(entries, entries=tuple(replace(e, content_sha256='abc') if e.relative_path.endswith('.zip') else e for e in entries.entries))
    result = plan(ctx, entries)
    assert by_source(result, src).target_relative_path == 'Duplicates/Show/Season 01/subs.zip'


def test_recycle_is_pruned_before_listing_and_not_in_coverage(tmp_path, monkeypatch):
    f, _ = api()
    recycle = tmp_path / '#recycle'
    recycle.mkdir()
    (recycle / 'secret.mkv').write_bytes(b'x')
    (tmp_path / 'visible.txt').write_text('x')
    real = f.os.scandir
    visited = []
    def guarded(path):
        visited.append(str(path))
        assert Path(path).name != '#recycle', 'entered system recycle bin'
        return real(path)
    monkeypatch.setattr(f.os, 'scandir', guarded)
    snap = f.inventory_filesystem(tmp_path)
    assert [(x.relative_path, x.kind) for x in snap.entries] == [('#recycle', 'system_excluded'), ('visible.txt', 'regular')]
    result = plan(replace(context(), videos=()), snap)
    assert dict(result.counts)['filesystem_visible_assets'] == 1
    assert dict(result.counts)['system_excluded_dirs'] == 1
    assert all(r.source_relative_path != '#recycle' for r in result.records)


def test_missing_sources_symlinks_and_unknowns_remain_visible(tmp_path):
    f, _ = api()
    (tmp_path/'link').symlink_to('/etc/passwd')
    (tmp_path/'foreign.bin').write_bytes(b'x')
    result = plan(context(), f.inventory_filesystem(tmp_path))
    assert by_source(result, 'old/source.mkv').status == 'BLOCKED'
    assert by_source(result, 'link').status == 'REVIEW'
    assert by_source(result, 'foreign.bin').status == 'REVIEW'
    assert len(result.records) == 3


@pytest.mark.parametrize('other_source,other_prefix,guard', [
    ('old/two.mkv', 'Show', 'exact'),
    ('old/two.mkv', 'show', 'casefold'),
    ('old/two.mkv', 'ſhow', 'uppercase'),
])
def test_collisions_guard_full_target_namespace(other_source, other_prefix, guard):
    _, t = api()
    ctx = context()
    # The filenames can differ while the directory spelling still collides.
    from app.physical_naming import NamingTitle
    names = dict(ctx.naming.titles)
    names[11] = NamingTitle(11,1,'season',1,None,True,('anilist','11'),other_prefix)
    ctx = replace(ctx, naming=replace(ctx.naming, titles=names), titles=ctx.titles+(t.TargetTitle(11,1,'season',1,None),),
                  videos=ctx.videos+(replace(ctx.videos[0],id=101,title_id=11,source=other_source),))
    result = plan(ctx, snapshot('old/source.mkv', other_source))
    assert any(c.guard == guard for c in result.collisions)
    assert all(r.status == 'BLOCKED' for r in result.records)


def test_variant_suffix_resolves_confirmed_lanes_without_collision():
    ctx = context(variant=1,label='BD')
    second = replace(ctx.videos[0],id=101,source='old/tv.mkv',variant_id=2,variant_label='TV')
    result = plan(replace(ctx,videos=ctx.videos+(second,)),snapshot('old/source.mkv','old/tv.mkv'))
    assert result.collisions == () and dict(result.status_counts)['READY'] == 2


def test_file_directory_conflict_is_blocked():
    ctx = context()
    source = 'Show/Season 01'
    result = plan(ctx, snapshot('old/source.mkv', source))
    assert any(c.guard == 'file_directory' for c in result.collisions)
    assert by_source(result,'old/source.mkv').status == 'BLOCKED'


def test_auxiliary_cannot_merge_into_managed_namespace():
    ctx = context(kind='bonus',content='bonus',season=None,layout='extras_bonus')
    source = 'old/Bonus/Track.flac'
    result = plan(ctx,snapshot('old/source.mkv',source))
    assert by_source(result,source).status == 'REVIEW'
    assert 'auxiliary_managed_namespace_collision' in by_source(result,source).blockers


def test_component_runtime_path_and_windows_budgets():
    f, _ = api()
    ctx = context()
    result = plan(ctx, snapshot('old/source.mkv', name_max=15, path_max=30))
    row = by_source(result,'old/source.mkv')
    assert row.status == 'BLOCKED'
    assert {'runtime_name_max','runtime_path_max'} <= set(row.blockers)
    assert result.windows_state == 'NOT_CHECKED / PRE_EXECUTION_REQUIRED'
    assert result.max_component_utf8_bytes == 17
    good = f.plan_library(ctx,snapshot('old/source.mkv'),windows_root='C:\\'+'a'*225)
    assert good.windows_state == 'WARNING'
    assert 'windows_path_budget' in good.records[0].warnings
    with pytest.raises(ValueError):
        f.plan_library(ctx,snapshot('old/source.mkv'),windows_root='relative')


def test_fresh_primary_regular_file_is_hard_safety_diagnostic():
    ctx = context()
    copy = replace(ctx.videos[0],id=101,source='old/copy.mkv',duplicate_primary_id=100,duplicate_validity='valid')
    ctx = replace(ctx,videos=ctx.videos+(copy,))
    absent = by_source(plan(ctx,snapshot(copy.source)),copy.source)
    assert absent.status == 'BLOCKED'
    assert absent.duplicate.primary_physical_state == 'MISSING'
    present = by_source(plan(ctx,snapshot('old/source.mkv',copy.source)),copy.source)
    assert present.duplicate.primary_physical_state == 'REGULAR_FILE'
    assert present.duplicate.purge_state == 'PRE_EXECUTION_REQUIRED'
    assert present.duplicate.secondary_in_duplicates is False


def test_plan_hash_and_snapshot_hash_are_stable_with_order_independent_evidence():
    ctx = context()
    snap = snapshot('old/source.mkv','seznam-souboru.txt')
    one = plan(ctx,snap)
    two = plan(ctx,replace(snap,entries=tuple(reversed(snap.entries))))
    assert one == two
    assert len(one.plan_hash) == 64 and len(one.filesystem_hash) == 64


def test_non_utf8_filesystem_filename_is_reported_without_crashing():
    source='old/Scan/invalid\udcff.png'
    result=plan(context(),snapshot('old/source.mkv',source))
    row=by_source(result,source)
    assert row.status == 'BLOCKED'
    assert 'invalid_logical_input' in row.blockers
    assert len(result.plan_hash)==64


def test_inventory_and_planner_do_not_call_any_filesystem_writer(tmp_path,monkeypatch):
    f,_=api()
    (tmp_path/'old').mkdir()
    (tmp_path/'old/source.mkv').write_bytes(b'x')
    (tmp_path/'old/source.ass').write_text('x')
    (tmp_path/'old/a.zip').write_bytes(b'zip')
    before=sorted((str(p.relative_to(tmp_path)),p.stat().st_size,p.stat().st_mtime_ns) for p in tmp_path.rglob('*'))
    def forbidden(*args,**kwargs):raise AssertionError('filesystem writer called')
    for name in ('rename','replace','unlink','remove','mkdir','chmod','utime','link','symlink'):
        monkeypatch.setattr(f.os,name,forbidden)
    real_open=f.os.open
    def readonly_open(path,flags,*args,**kwargs):
        assert not flags & (f.os.O_WRONLY|f.os.O_RDWR|f.os.O_CREAT|f.os.O_TRUNC)
        return real_open(path,flags,*args,**kwargs)
    monkeypatch.setattr(f.os,'open',readonly_open)
    result=plan(context(),f.inventory_filesystem(tmp_path))
    assert len(result.records)==3
    assert before==sorted((str(p.relative_to(tmp_path)),p.stat().st_size,p.stat().st_mtime_ns) for p in tmp_path.rglob('*'))


def test_duplicate_side_accounting_does_not_assume_auxiliary_ownership():
    ctx=context()
    copy=replace(ctx.videos[0],id=101,source='old/copy/copy.mkv',duplicate_primary_id=100,duplicate_validity='valid')
    ctx=replace(ctx,videos=ctx.videos+(copy,))
    result=plan(ctx,snapshot('old/source.mkv',copy.source,'old/copy/Scan/image.png'))
    assert by_source(result,'old/copy/Scan/image.png').object_kind=='auxiliary'
    assert by_source(result,copy.source).duplicate.side_assets_accounted is False


def test_duplicate_accounting_reflects_final_side_asset_collision():
    _,t=api()
    ctx=context()
    copy=replace(ctx.videos[0],id=101,source='old/copy/copy.mkv',duplicate_primary_id=100,duplicate_validity='valid')
    paths=('old/copy/sub.zip','old/copy/SUB.zip')
    ctx=replace(ctx,videos=ctx.videos+(copy,),side_assets=tuple(t.SideAssetEvidence(p,101,'explicit_secondary') for p in paths))
    result=plan(ctx,snapshot('old/source.mkv',copy.source,*paths))
    assert by_source(result,paths[0]).status=='BLOCKED'
    assert by_source(result,copy.source).duplicate.side_assets_accounted is False


def test_recycle_named_regular_file_remains_in_coverage(tmp_path):
    f,_=api()
    (tmp_path/'#recycle').write_bytes(b'x')
    snap=f.inventory_filesystem(tmp_path)
    assert snap.entries[0].kind=='regular'
    result=plan(replace(context(),videos=()),snap)
    assert dict(result.counts)['filesystem_visible_assets']==1
    assert dict(result.counts)['system_excluded_dirs']==0
    assert by_source(result,'#recycle').status=='REVIEW'


def test_duplicate_side_assets_belong_only_to_their_own_secondary():
    _,t=api()
    ctx=context()
    second=replace(ctx.videos[0],id=102,source='old/ep2.mkv',episode_number=2)
    copies=(replace(ctx.videos[0],id=101,source='old/copy/a.mkv',duplicate_primary_id=100,duplicate_validity='valid'),
            replace(second,id=103,source='old/copy/b.mkv',duplicate_primary_id=102,duplicate_validity='valid'))
    subs=(t.TargetSubtitle(201,'old/copy/a.ass',((101,'confirmed_compatible'),)),
          t.TargetSubtitle(203,'old/copy/b.ass',((103,'automatic_match'),)))
    ctx=replace(ctx,videos=(ctx.videos[0],second)+copies,subtitles=subs)
    result=plan(ctx,snapshot('old/source.mkv','old/ep2.mkv','old/copy/a.mkv','old/copy/b.mkv','old/copy/a.ass','old/copy/b.ass'))
    a=by_source(result,'old/copy/a.mkv').duplicate
    b=by_source(result,'old/copy/b.mkv').duplicate
    assert a.side_assets==('old/copy/a.ass',) and b.side_assets==('old/copy/b.ass',)
    assert a.side_assets_accounted is True and b.side_assets_accounted is True
    assert by_source(result,'old/copy/b.ass').target_relative_path=='Duplicates/Show/Season 01/b.ass'


@pytest.mark.parametrize('extra', [(), ('seznam-souboru.txt',)])
def test_library_root_secondary_is_never_accounted_without_inspection(extra):
    ctx=context()
    copy=replace(ctx.videos[0],id=101,source='copy.mkv',duplicate_primary_id=100,duplicate_validity='valid')
    ctx=replace(ctx,videos=ctx.videos+(copy,))
    result=plan(ctx,snapshot('old/source.mkv','copy.mkv',*extra))
    row=by_source(result,'copy.mkv')
    assert row.target_relative_path=='Duplicates/Show/Season 01/copy.mkv' and row.status=='READY'
    assert row.duplicate.side_assets_accounted is False
    assert row.duplicate.side_asset_accounting == 'UNKNOWN'
    assert row.duplicate.purge_state=='PRE_EXECUTION_REQUIRED'


def test_inventory_lists_each_directory_exactly_once(tmp_path,monkeypatch):
    f,_=api()
    for rel in ('a/b/c.mkv','a/d.ass','e/f/g/h.flac','root.txt'):
        (tmp_path/rel).parent.mkdir(parents=True,exist_ok=True);(tmp_path/rel).write_bytes(b'x')
    real=f.os.scandir
    listed=[]
    monkeypatch.setattr(f.os,'scandir',lambda path:(listed.append(Path(path).relative_to(tmp_path).as_posix()),real(path))[1])
    snap=f.inventory_filesystem(tmp_path)
    directories=[e.relative_path for e in snap.entries if e.kind=='directory']
    assert sorted(listed)==sorted(['.',*directories]) and len(listed)==len(set(listed))==6
