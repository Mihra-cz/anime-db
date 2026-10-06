"""Targets are projections of existing IDs/authority, never new ownership."""
from dataclasses import replace, FrozenInstanceError
from datetime import datetime, timezone
from decimal import Decimal
import importlib
import importlib.util
from types import MappingProxyType

import pytest

from app.physical_naming import NamingChoice, NamingTitle, PhysicalNamingContext, create_basis_snapshot
from app.physical_layout import LayoutTitle, LayoutAttachment, LayoutChoice, PhysicalLayoutContext
from app.physical_layout import create_basis_snapshot as layout_basis

NOW = datetime(2026, 10, 6, tzinfo=timezone.utc)


def api():
    assert importlib.util.find_spec('app.target_planner') is not None, 'Target Planner foundation missing'
    return importlib.import_module('app.target_planner'), importlib.import_module('app.target_planner_types')


def context(*, kind='season', season=1, part=None, layout=None, label=None, variant=None, content='episode', ordinal=None, mp=None):
    _, t = api()
    names = PhysicalNamingContext({1: (10,)}, {10: NamingTitle(10, 1, kind, season, part, True, ('anilist', '1'), 'Show')}, {})
    choices = {('collection', 1): NamingChoice(1, 'Show', 'custom', NOW, create_basis_snapshot(names, 'collection', 1))}
    names = replace(names, choices=MappingProxyType(choices))
    layouts = PhysicalLayoutContext({10: LayoutTitle(10, 1, kind, LayoutAttachment('season' if season else 'root', season), True, (content,), 'typed_content', None, 1)}, {})
    if layout:
        layouts = replace(layouts, choices={10: LayoutChoice(1, layout, NOW, layout_basis(layouts, 10))})
    return t.PlannerContext(names, layouts,
        (t.SourceCollection(1, ('old',)),),
        (t.TargetTitle(10, 1, kind, season, part),),
        (t.TargetVideo(100, 10, 1, 'old/source.mkv', content, episode_number=1 if content == 'episode' else None,
                       ordinal=ordinal, media_part=mp, variant_id=variant, variant_label=label),), (), ())


def record(ctx, kind='video', id=100):
    p, _ = api()
    return next(r for r in p.project_targets(ctx) if r.object_kind == kind and r.object_id == id)


@pytest.mark.parametrize('kind,season,part,content,layout,ordinal,want', [
    ('season', 1, None, 'episode', None, None, 'Show/Season 01/Show - S01E01.mkv'),
    ('part', 2, 1, 'episode', None, None, 'Show/Season 02/Show - S02P01E01.mkv'),
    ('film', None, None, 'film', None, None, 'Show/Show - Film.mkv'),
    ('film', 2, None, 'film', None, None, 'Show/Season 02/Show - S02 - Film.mkv'),
    ('bonus', 1, None, 'bonus', 'extras_bonus', 2, 'Show/Season 01/Extras/Bonus/Show - S01 - Bonus 02.mkv'),
    ('bonus', 1, None, 'cm', 'extras_promo', 1, 'Show/Season 01/Extras/CM/Show - S01 - CM 01.mkv'),
    ('bonus', 1, None, 'menu', 'extras_menus', 1, 'Show/Season 01/Extras/Menu/Show - S01 - Menu 01.mkv'),
    ('bonus', 1, None, 'bonus', 'own_folder', 2, 'Show/Season 01/Show/Show - S01 - Bonus 02.mkv'),
    ('ova', None, None, 'ova', 'shared_ova', None, 'Show/OVA/Show - OVA.mkv'),
])
def test_authoritative_placement(kind, season, part, content, layout, ordinal, want):
    result = record(context(kind=kind, season=season, part=part, content=content, layout=layout, ordinal=ordinal))
    assert result.target_relative_path == want
    assert result.status == 'READY' and result.action == 'MOVE'


def test_shared_parts_and_season_only_supplementary_do_not_make_part_directories():
    p, t = api()
    ctx = context(kind='part', season=2, part=1)
    titles = ctx.titles + (t.TargetTitle(11, 1, 'part', 2, 2), t.TargetTitle(12, 1, 'bonus', 2, None), t.TargetTitle(13, 1, 'preview', 4, None))
    name_titles = dict(ctx.naming.titles)
    for title in titles[1:]:
        name_titles[title.id] = NamingTitle(title.id, 1, title.kind, title.season, title.part, True, ('anilist', str(title.id)), 'Show')
    names = replace(ctx.naming, collections={1: tuple(x.id for x in titles)}, titles=name_titles)
    names = replace(names, choices={('collection', 1): replace(
        names.choices[('collection', 1)], basis_snapshot_json=create_basis_snapshot(names, 'collection', 1))})
    layouts = dict(ctx.layout.titles)
    for title, content in [(titles[2], 'ncop'), (titles[3], 'preview')]:
        layouts[title.id] = LayoutTitle(title.id, 1, title.kind, LayoutAttachment('season', title.season), True, (content,), 'story_preview' if content=='preview' else 'typed_content', None, 1)
    videos = ctx.videos + (
        t.TargetVideo(101, 11, 1, 'old/p2.mkv', 'episode', episode_number=1),
        t.TargetVideo(102, 12, 1, 'old/nc.mkv', 'ncop'),
        t.TargetVideo(103, 13, 1, 'old/reflection.mkv', 'preview'),
    )
    results = p.project_targets(replace(ctx, titles=titles, naming=names, layout=replace(ctx.layout, titles=layouts), videos=videos))
    assert [r.target_relative_path for r in results] == [
        'Show/Season 02/Show - S02P01E01.mkv', 'Show/Season 02/Show - S02P02E01.mkv',
        'Show/Season 02/Extras/Openings & Endings/Show - S02 - NCOP.mkv',
        'Show/Season 04/Show - S04 - Preview.mkv',
    ]


@pytest.mark.parametrize('label,want', [('BD', 'BD'), ('TV', 'TV'), ("Director's cut", "Director's cut"), ('日本語: café/版?', '日本語 - café-版')])
def test_variant_membership_always_has_manual_suffix(label, want):
    r = record(context(variant=5, label=label, mp=2))
    assert r.target_relative_path == f'Show/Season 01/Show - S01E01-MP02 [{want}].mkv'
    assert 'variant:5' in r.authority


@pytest.mark.parametrize('label', [None, '', ' / ', '\x00'])
def test_missing_unsafe_variant_label_never_gets_default(label):
    r = record(context(variant=5, label=label))
    assert r.target_relative_path is None and r.status in ('REVIEW', 'BLOCKED')
    assert 'variant_label_unavailable' in r.blockers


def test_variant_like_source_filename_is_not_authority():
    ctx = context()
    ctx = replace(ctx, videos=(replace(ctx.videos[0], source='old/BD TV Episode.mkv'),))
    assert record(ctx).target_relative_path == 'Show/Season 01/Show - S01E01.mkv'


def test_duplicate_secondary_uses_primary_placement_and_original_filename():
    p, t = api()
    ctx = context()
    copy = replace(ctx.videos[0], id=101, source='old/Original copy.MKV', duplicate_primary_id=100, duplicate_validity='valid')
    ctx = replace(ctx, videos=ctx.videos + (copy,))
    r = record(ctx, id=101)
    assert r.target_relative_path == 'Duplicates/Show/Season 01/Original copy.MKV'
    assert r.action == 'QUARANTINE'
    assert record(ctx).target_relative_path == 'Show/Season 01/Show - S01E01.mkv'
    assert r.duplicate.secondary_video_id == 101 and r.duplicate.primary_video_id == 100
    assert r.duplicate.primary_source_relative_path == 'old/source.mkv'
    assert r.duplicate.primary_target_relative_path == 'Show/Season 01/Show - S01E01.mkv'
    assert r.duplicate.validity == 'valid'
    assert r.duplicate.purge_state == 'PRE_EXECUTION_REQUIRED'


@pytest.mark.parametrize('validity', ['invalid', 'unknown'])
def test_invalid_duplicate_is_review_with_preserved_relation(validity):
    ctx = context()
    ctx = replace(ctx, videos=(replace(ctx.videos[0], duplicate_primary_id=999, duplicate_validity=validity),))
    r = record(ctx)
    assert r.target_relative_path is None and r.status == 'REVIEW'
    assert r.duplicate.primary_video_id == 999 and r.duplicate.validity == validity


def test_duplicate_subtitle_has_quarantine_stem_and_explicit_edges():
    _, t = api()
    ctx = context()
    copy = replace(ctx.videos[0], id=101, source='old/Original copy.MKV', duplicate_primary_id=100, duplicate_validity='valid')
    sub = t.TargetSubtitle(200, 'old/Original copy.ass', ((101, 'confirmed_compatible'),))
    ctx = replace(ctx, videos=ctx.videos+(copy,), subtitles=(sub,))
    r = record(ctx, 'subtitle', 200)
    assert r.target_relative_path == 'Duplicates/Show/Season 01/Original copy.ass'
    assert r.action == 'QUARANTINE' and r.compatibility == ((101, 'confirmed_compatible'),)
    assert r.duplicate.secondary_video_id == 101


def test_m_to_n_subtitle_has_one_bd_target_and_retains_both_edges():
    p, t = api()
    ctx = context(variant=1, label='TV')
    tv = replace(ctx.videos[0], release_source='tv')
    bd = replace(tv, id=101, source='old/bd.mkv', variant_id=2, variant_label='BD', release_source='bd')
    edges = ((100, 'confirmed_compatible'), (101, 'automatic_match'))
    ctx = replace(ctx, videos=(tv, bd), subtitles=(t.TargetSubtitle(200, 'old/ep.ass', edges),))
    results = p.project_targets(ctx)
    subs = [r for r in results if r.object_kind == 'subtitle']
    assert len(subs) == 1
    assert subs[0].target_relative_path == 'Show/Season 01/Show - S01E01 [BD].ass'
    assert subs[0].compatibility == edges


def test_ambiguous_many_to_many_subtitle_requires_review():
    _, t = api()
    ctx = context()
    other = replace(ctx.videos[0], id=101, episode_number=2, source='old/ep2.mkv')
    sub = t.TargetSubtitle(200, 'old/sub.ass', ((100, 'confirmed_compatible'), (101, 'confirmed_compatible')))
    r = record(replace(ctx, videos=ctx.videos+(other,), subtitles=(sub,)), 'subtitle', 200)
    assert r.status == 'REVIEW' and r.target_relative_path is None


def test_no_match_ova_without_season_evidence_preserves_root_original():
    _, t = api()
    ctx = context()
    ctx = replace(ctx, unmatched=(t.UnmatchedSubtitle(200, 'old/Show - OVA 01.ass', 'confirmed_no_match'),))
    r = record(ctx, 'confirmed_no_match', 200)
    assert r.target_relative_path == 'Show/Show - OVA 01.ass'
    assert r.status == 'WARNING' and 'subtitle_present_video_missing' in r.warnings
    assert r.compatibility == ()


def test_no_match_safe_continuation_requires_exact_confirmed_series():
    p, t = api()
    ctx = context(season=2)
    ctx = replace(ctx, videos=tuple(replace(ctx.videos[0], id=100+n, source=f'old/Series S2 - {n:02}.mkv', episode_number=n) for n in (10,11,12)),
                  subtitles=tuple(t.TargetSubtitle(200+n, f'old/Series S2 - {n:02}.ass', ((100+n,'confirmed_compatible'),)) for n in (10,11,12)),
                  unmatched=(t.UnmatchedSubtitle(300, 'old/Series S2 - 13.ass', 'confirmed_no_match'),))
    r = record(ctx, 'confirmed_no_match', 300)
    assert r.target_relative_path == 'Show/Season 02/Show - S02E13.ass'
    assert 'confirmed_series_continuation' in r.authority
    unsafe = replace(ctx, subtitles=(ctx.subtitles[0],))
    assert record(unsafe, 'confirmed_no_match', 300).target_relative_path == 'Show/Series S2 - 13.ass'


def series(numbers=(10, 11, 12), orphan='old/Series S2 - 13.ass', titles=(), videos=()):
    _, t = api()
    ctx = context(season=2)
    confirmed = tuple(replace(ctx.videos[0], id=100+n, source=f'old/Series S2 - {n:02}.mkv', episode_number=n) for n in numbers)
    return replace(ctx, titles=ctx.titles+titles, videos=confirmed+videos,
                   subtitles=tuple(t.TargetSubtitle(200+n, f'old/Series S2 - {n:02}.ass', ((100+n, 'confirmed_compatible'),)) for n in numbers),
                   unmatched=(t.UnmatchedSubtitle(300, orphan, 'confirmed_no_match'),))


@pytest.mark.parametrize('case', [
    'second_season', 'second_part', 'existing_next_video', 'existing_later_video', 'gap_inside_series', 'gap_before_orphan', 'zero_padding_only', 'different_suffix', 'different_extension',
])
def test_no_match_continuation_rejects_every_ambiguous_context(case):
    _, t = api()
    video = lambda id, n, title=10: t.TargetVideo(id, title, 1, f'old/Other {n}.mkv', 'episode', episode_number=n)
    ctx = {
        'second_season': lambda: series(titles=(t.TargetTitle(11, 1, 'season', 3, None),)),
        'second_part': lambda: series(titles=(t.TargetTitle(11, 1, 'part', 2, 2),)),
        'existing_next_video': lambda: series(videos=(video(113, 13),)),
        'existing_later_video': lambda: series(videos=(video(114, 14),)),
        'gap_inside_series': lambda: series(numbers=(9, 10, 12)),
        'gap_before_orphan': lambda: series(orphan='old/Series S2 - 14.ass'),
        'zero_padding_only': lambda: series(orphan='old/Series S2 - 013.ass'),
        'different_suffix': lambda: series(orphan='old/Series S2 - 13 v2.ass'),
        'different_extension': lambda: series(orphan='old/Series S2 - 13.srt'),
    }[case]()
    r = record(ctx, 'confirmed_no_match', 300)
    assert r.target_relative_path == 'Show/' + ctx.unmatched[0].source.rsplit('/', 1)[1]
    assert 'confirmed_series_continuation' not in r.authority
    assert r.status == 'WARNING' and r.warnings == ('subtitle_present_video_missing',)


def test_no_match_continuation_keeps_series_padding_and_single_context():
    _, t = api()
    ctx = series(numbers=tuple(range(1, 16)), orphan='old/Series S2 - 16.ass',
                 titles=(t.TargetTitle(12, 1, 'ova', None, None),))
    r = record(ctx, 'confirmed_no_match', 300)
    assert r.target_relative_path == 'Show/Season 02/Show - S02E16.ass'
    assert 'confirmed_series_continuation' in r.authority


def test_projection_is_immutable_and_deterministic_without_filesystem():
    p, _ = api()
    ctx = context()
    assert p.project_targets(ctx) == p.project_targets(ctx)
    with pytest.raises(FrozenInstanceError):
        record(ctx).target_relative_path = 'changed'


def test_missing_number_or_structural_authority_never_gets_guessed():
    ctx = context()
    assert record(replace(ctx, videos=(replace(ctx.videos[0], episode_number=None),))).status == 'REVIEW'
    assert record(replace(ctx, titles=(replace(ctx.titles[0], issues=('incomplete_manual_snapshot',)),))).target_relative_path is None


def test_exact_recap_position_and_mp_are_preserved():
    ctx = context(content='recap', mp=2)
    ctx = replace(ctx, videos=(replace(ctx.videos[0], recap_position=Decimal('24.25')),))
    assert record(ctx).target_relative_path == 'Show/Season 01/Show - S01 - Recap 24.25-MP02.mkv'


@pytest.mark.parametrize('reserved', ['#recycle', 'Duplicates', 'subs', 'DUPLICATES'])
def test_collection_name_cannot_enter_system_or_preservation_lane(reserved):
    ctx = context()
    choice = replace(ctx.naming.choices[('collection',1)],physical_text=reserved)
    ctx = replace(ctx,naming=replace(ctx.naming,choices={('collection',1):choice}))
    r = record(ctx)
    assert r.status == 'REVIEW' and r.target_relative_path is None
    assert 'reserved_library_namespace' in r.blockers


@pytest.mark.parametrize('additional', ['unassigned','web','extra_tv'])
def test_m_to_n_unapproved_representation_combinations_stay_review(additional):
    _,t=api()
    ctx=context(variant=1,label='BD')
    bd=replace(ctx.videos[0],release_source='bd')
    other=replace(bd,id=101,source='old/other.mkv',variant_id=None if additional=='unassigned' else 2,
                  variant_label=None if additional=='unassigned' else 'WEB' if additional=='web' else 'TV',
                  release_source=None if additional=='unassigned' else 'web' if additional=='web' else 'tv')
    videos=(bd,other)
    if additional=='extra_tv':videos+=(replace(other,id=102,source='old/extra.mkv',variant_id=3,variant_label='Other TV'),)
    sub=t.TargetSubtitle(200,'old/shared.ass',tuple((v.id,'confirmed_compatible') for v in videos))
    r=record(replace(ctx,videos=videos,subtitles=(sub,)),'subtitle',200)
    assert r.target_relative_path is None and r.status=='REVIEW'


def test_missing_video_subtitle_never_continues_duplicate_quarantine_series():
    _,t=api()
    ctx=context()
    primaries=tuple(replace(ctx.videos[0],id=100+n,source=f'old/primary/Series - {n:02}.mkv',episode_number=n) for n in (10,11,12))
    copies=tuple(replace(v,id=v.id+100,source=f'old/copy/Series - {v.episode_number:02}.mkv',duplicate_primary_id=v.id,duplicate_validity='valid') for v in primaries)
    subs=tuple(t.TargetSubtitle(300+v.episode_number,f'old/copy/Series - {v.episode_number:02}.ass',((v.id,'confirmed_compatible'),)) for v in copies)
    ctx=replace(ctx,videos=primaries+copies,subtitles=subs,unmatched=(t.UnmatchedSubtitle(500,'old/copy/Series - 13.ass','confirmed_no_match'),))
    assert record(ctx,'confirmed_no_match',500).target_relative_path=='Show/Series - 13.ass'
