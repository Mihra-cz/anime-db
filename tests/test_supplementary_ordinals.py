import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, raiseload

from app.catalog import classify_video, detect_episode_number, effective_video_content_display
from app.database import Base
from app.models import CatalogTitle, Video
from app.numbering import effective_video_numbering, video_numbering_identity, unresolved_duplicate_groups
from app.supplementary import (
    supplementary_inventory,
    supplementary_ordinal,
    supplementary_review_issues,
)
from app.metadata.candidates import local_episode_count_evidence


def graph(kind='bonus'):
    return CatalogTitle(id=10, local_title='Show', normalized_local_title='show',
                        relative_root_path='Show', part_type=kind, season_number=1)


def video(title, filename, identifier=1, **kwargs):
    return Video(id=identifier, catalog_title=title, catalog_title_id=title.id,
                 filename=filename, relative_path=f'Show/{identifier}/{filename}',
                 root_folder='Show', size=1, mtime_ns=1,
                 file_type=classify_video(filename), **kwargs)


@pytest.mark.parametrize('kind,filename,subtype,number', [
    ('ova', 'OVA01.mkv', 'ova', 1), ('ova', 'OVA02.mkv', 'ova', 2),
    ('bonus', 'NCOP01.mkv', 'ncop', 1), ('bonus', 'NCOP02.mkv', 'ncop', 2),
    ('special', 'NCED04.mkv', 'nced', 4), ('season', 'NCOP02.mkv', 'ncop', 2),
    ('special', 'Special 01.mkv', 'special', 1), ('bonus', 'OP01.mkv', 'op', 1),
    ('bonus', 'ED02.mkv', 'ed', 2), ('bonus', 'Preview 02.mkv', 'preview', 2),
    ('bonus', 'PV02.mkv', 'preview', 2), ('bonus', 'CM03.mkv', 'cm', 3),
])
def test_parser_ordinal_survives_classification_and_presentation(kind, filename, subtype, number):
    title = graph(kind)
    item = video(title, filename)
    result = supplementary_ordinal(item)
    state = effective_video_numbering(item)
    assert (result.supplementary_type, result.number, result.source) == (subtype, number, 'parser')
    assert (state.supplementary_type, state.supplementary_number) == (subtype, number)
    assert video_numbering_identity(item).number == number
    assert effective_video_content_display(item).display_label == result.display_label
    assert item.season_episode_number is None


def test_manual_number_wins_in_every_consumer():
    item = video(graph('ova'), 'OVA03.mkv', episode_number_manual_override=1)
    assert supplementary_ordinal(item).number == 1
    assert effective_video_numbering(item).supplementary_number == 1
    assert video_numbering_identity(item).number == 1
    assert effective_video_content_display(item).display_label == 'OVA 01'


@pytest.mark.parametrize('manual', ['other', 'film', 'recap', 'special'])
def test_manual_classification_cancels_incompatible_parser_identity(manual):
    item = video(graph(), 'NCOP03.mkv', content_type_manual=manual)
    state = supplementary_ordinal(item)
    assert state is None or (state.supplementary_type == manual and state.number is None)
    assert effective_video_numbering(item).supplementary_number is None
    assert video_numbering_identity(item) is None
    assert 'NCOP' not in effective_video_content_display(item).display_label


@pytest.mark.parametrize('raw', ['episode', 'ova'])
def test_source_episode_14_is_not_ova_14(raw):
    item = video(graph('ova'), 'Episode 14.mkv', season_episode_number=14,
                 external_episode_number=14, local_episode_number=14)
    item.file_type = raw
    assert supplementary_ordinal(item).number is None
    assert effective_video_numbering(item).supplementary_number is None
    assert supplementary_inventory([item]).requires_review
    item.episode_number_manual_override = 1
    assert supplementary_ordinal(item).number == 1


def test_media_parts_are_one_ordinal_and_multiple_ordinals_stay_separate():
    title = graph('ova')
    items = [video(title, f'OVA0{n} Part {p} of 2.mkv', n * 10 + p, media_part_number=p)
             for n in (1, 2) for p in (1, 2)]
    assert [detect_episode_number(v.filename).supplementary_number for v in items] == [1, 1, 2, 2]
    assert supplementary_inventory(items).logical_count == 2
    assert unresolved_duplicate_groups(items) == ()
    assert local_episode_count_evidence(title).count == 2


def test_legacy_ova_p_is_physical_evidence_when_media_part_authority_exists():
    item = video(graph('ova'), 'OVA P2.mkv', media_part_number=2)
    assert detect_episode_number(item.filename).supplementary_number == 2
    assert supplementary_ordinal(item).number is None
    item.episode_number_manual_override = 1
    assert supplementary_ordinal(item).number == 1


def test_variants_and_duplicate_copy_do_not_increase_logical_count():
    title = graph()
    items = [video(title, f'NCOP{n:02d}.mkv', n * 10 + lane, video_variant_group_id=lane)
             for n in (1, 2) for lane in (1, 2)]
    duplicate = video(title, 'NCOP01.mkv', 99, duplicate_of_video_id=11, video_variant_group_id=1)
    items.append(duplicate)
    assert supplementary_inventory(items).logical_count == 2
    assert unresolved_duplicate_groups(items) == ()
    assert local_episode_count_evidence(title).count == 2


@pytest.mark.parametrize('lanes', [(None, None), (1, None), (1, 1)])
def test_unconfirmed_collision_is_review_not_a_logical_count(lanes):
    title = graph('ova')
    items = [video(title, 'OVA01.mkv', i, video_variant_group_id=lane)
             for i, lane in enumerate(lanes, 1)]
    result = supplementary_inventory(items)
    assert result.requires_review and result.logical_count is None
    assert len(unresolved_duplicate_groups(items)) == 1
    assert local_episode_count_evidence(title).count is None


def test_future_rename_precondition_unknowns_and_namespaces():
    title = graph()
    items = [video(title, 'NCOP.mkv', i) for i in (1, 2)]
    assert len(supplementary_inventory(items).unknown_videos) == 2
    assert supplementary_inventory(items).requires_review
    items = [video(title, 'OP01.mkv', 3), video(title, 'ED01.mkv', 4)]
    assert supplementary_inventory(items).logical_count == 2


def test_hierarchy_review_reasons_distinguish_missing_collision_and_broken_identity():
    title = graph('special')
    missing = [video(title, name, identifier) for identifier, name in (
        (1, 'Special.mkv'), (2, 'Special alternate.mkv'),
    )]
    missing_issues = supplementary_review_issues(missing, title)
    assert [issue.code for issue in missing_issues] == [
        'missing_supplementary_ordinal',
    ]
    assert missing_issues[0].known_ordinals == ()
    assert missing_issues[0].videos == tuple(missing)

    collision = [
        video(title, 'NCED01.mkv', 3),
        video(title, 'Show NCED01.mkv', 4),
    ]
    collision_issues = supplementary_review_issues(collision, title)
    assert [issue.code for issue in collision_issues] == [
        'supplementary_ordinal_collision',
    ]
    assert collision_issues[0].known_ordinals == (1,)

    broken = video(title, 'NCOP01.mkv', 5, duplicate_of_video_id=999)
    broken_issues = supplementary_review_issues([broken], title)
    assert [issue.code for issue in broken_issues] == [
        'broken_supplementary_identity',
    ]


def test_hierarchy_review_reason_disappears_when_ordinals_or_variants_resolve_identity():
    title = graph('ova')
    unknown = [video(title, 'OVA.mkv', 1), video(title, 'OVA alternate.mkv', 2)]
    assert supplementary_review_issues(unknown, title)
    unknown[0].episode_number_manual_override = 1
    unknown[1].episode_number_manual_override = 2
    assert supplementary_review_issues(unknown, title) == ()

    variants = [
        video(title, 'OVA01.mkv', 3, video_variant_group_id=1),
        video(title, 'Show OVA01.mkv', 4, video_variant_group_id=2),
    ]
    assert supplementary_review_issues(variants, title) == ()


@pytest.mark.parametrize('filename,kind,number', [
    ('Menu01.mkv', 'menu', 1), ('Bonus01.mkv', 'bonus', 1),
    ('Interview 01.mkv', 'bonus', None), ('Other 01.mkv', 'bonus', None),
])
def test_generalized_namespace_uses_only_compatible_number_evidence(filename, kind, number):
    state = supplementary_ordinal(video(graph(), filename))
    assert (state.supplementary_type, state.number) == (kind, number)


def test_nande_tv_sequence_recognition_does_not_create_variant_authority():
    item = video(graph(), 'Nande - NCOP Ver.TV1.mp4', video_variant_group_id=9)
    assert supplementary_ordinal(item).number == 1
    assert item.video_variant_group_id == 9


def test_missing_duplicate_primary_stays_review():
    item = video(graph(), 'NCOP01.mkv', duplicate_of_video_id=999)
    result = supplementary_inventory([item])
    assert result.invalid_duplicates == (item,)
    assert result.logical_count is None


def test_resolver_is_in_memory_without_lazy_queries_or_writes(tmp_path):
    engine = create_engine(f'sqlite:///{tmp_path / "ordinal.db"}')
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        title = graph()
        items = [video(title, f'NCOP{i:02d}.mkv', i) for i in range(1, 101)]
        session.add_all(items)
        session.commit()
    with Session(engine) as session:
        items = list(session.scalars(select(Video).options(raiseload('*'))))
        statements = []
        def record(conn, cursor, statement, parameters, context, many):
            statements.append(statement)
        event.listen(engine, 'before_cursor_execute', record)
        assert supplementary_inventory(items).logical_count == 100
        assert supplementary_review_issues(items, graph()) == ()
        assert not session.dirty
        assert statements == []
        event.remove(engine, 'before_cursor_execute', record)


def test_title_scope_and_unloaded_title_previews():
    first, second = graph('ova'), graph('ova')
    second.id = 20
    a, b = video(first, 'OVA01.mkv'), video(second, 'OVA01.mkv', 2)
    assert supplementary_inventory([a, b]).logical_count == 2
    item = video(first, 'Episode 14.mkv', 3)
    assert supplementary_ordinal(item).number is None
    assert supplementary_ordinal(item, use_current_title=False) is None
    assert effective_video_numbering(item, use_current_title=False).is_standard


def test_media_part_labels_are_scoped_to_ordinal_and_variant():
    from app.media_parts import media_part_label
    from app.supplementary import supplementary_media_siblings
    title = graph('ova')
    items = [video(title, f'OVA{n:02d} Part {p}.mkv', n * 100 + lane * 10 + p,
                   media_part_number=p, video_variant_group_id=lane)
             for n in (1, 2) for lane in (1, 2) for p in (1, 2)]
    siblings = supplementary_media_siblings(items)
    assert supplementary_inventory(items).logical_count == 2
    assert all(media_part_label(v, siblings[v]) == f'Část média {v.media_part_number}/2' for v in items)


def test_unknown_prefix_and_release_directory_do_not_hide_collision():
    title = graph('bonus')
    title.season_number = None
    a = video(title, 'Show uncensored NCOP01.mkv')
    b = video(title, 'Show censored NCOP01.mkv', 2)
    assert len(unresolved_duplicate_groups([a, b])) == 1


@pytest.mark.parametrize('count', [10, 100])
def test_inventory_parser_work_is_linear(monkeypatch, count):
    import app.supplementary as resolver
    title = graph()
    items = [video(title, f'NCOP{n:03d}.mkv', n) for n in range(1, count + 1)]
    original = resolver.detect_episode_number
    calls = []
    def detect(filename):
        calls.append(filename)
        return original(filename)
    monkeypatch.setattr(resolver, 'detect_episode_number', detect)
    assert resolver.supplementary_inventory(items).logical_count == count
    assert len(calls) == count


def test_broad_scanner_substring_is_not_safe_ordinal_evidence():
    item = video(graph('bonus'), 'Syncope Episode 14.mkv')
    item.file_type = 'ncop'
    assert supplementary_ordinal(item).number is None


@pytest.mark.parametrize('filename,subtype,number,hint,marker', [
    ('Nande - NCOP Ver.TV1.mp4', 'ncop', 1, 'Ver.TV', None),
    ('Nande - NCOP Ver.TV4.mp4', 'ncop', 4, 'Ver.TV', None),
    ('Show NCED Ver.TV2.mkv', 'nced', 2, 'Ver.TV', None),
    ('Show OP Ver.TV1.mkv', 'op', 1, 'Ver.TV', None),
    ('Show ED Ver.TV4.mkv', 'ed', 4, 'Ver.TV', None),
    ('[Beatrice-Raws] Tenki no Ko (PV 01) [BDRip 1920x1080 HEVC FLAC].mkv', 'preview', 1, None, None),
    ('Show (PV 03) [1080p x265].mkv', 'preview', 3, None, None),
    ('high_scool_dxd_bd_spec_02.mp4', 'special', 2, None, None),
    ('high_scool_dxd_bd_spec_06.mp4', 'special', 6, None, None),
    ('[Judas] Show - NCED 02a.mkv', 'nced', 2, None, 'A'),
    ('[Judas] Show - NCED 02b.mkv', 'nced', 2, None, 'B'),
    ('PV01.mkv', 'preview', 1, None, None),
    ('PV02.mkv', 'preview', 2, None, None),
])
def test_audited_supplementary_patterns(filename, subtype, number, hint, marker):
    detection = detect_episode_number(filename)
    assert (detection.kind, detection.supplementary_type, detection.supplementary_number) == ('supplementary', subtype, number)
    assert (detection.version_hint, detection.structural_marker) == (hint, marker)
    item = video(graph(), filename)
    assert supplementary_ordinal(item).number == number
    assert item.season_episode_number is None
    assert item.video_variant_group_id is None
    assert item.media_part_number is None


@pytest.mark.parametrize('filename', [
    'Show Ver.TV1.mkv', 'Show OVA Ver.TV1.mkv', 'Show Special Ver.TV4.mkv',
    'Show NCOP Ver.TV1080.mkv', 'Show NCOP Ver.TV264.mkv',
    'Show NCOP Ver.TV1 [1080p x265].mkv', 'Show NCOP Ver.TV1.2.mkv',
    'Show NCOP [Ver.TV1].mkv', 'Show NCOP Ver.TV x265.mkv',
    'Show NCOP Ver.TV1v2.mkv', 'Show NCOP Ver.TV1 Part 2.mkv',
    'Show NCOP [1920x1080] [x265] v2.mkv',
    'Show (PV) [BDRip 1920x1080 HEVC FLAC].mkv', 'Show (PV x265).mkv',
    'Show (PV 01 v2).mkv', 'Show PV [1080p].mkv',
    'Show_bd_spec_Vol_02.mkv', 'Show_bd_spec_1080.mkv', 'Show_bd_spec_02v2.mkv',
    'Show spec_02.mkv', 'Show bd spec 02.mkv', 'Show_bdspec_02.mkv',
    'Show NCED 02bit.mkv', 'Show NCED 02av1.mkv',
])
def test_nearby_release_or_unsupported_patterns_do_not_supply_ordinal(filename):
    detection = detect_episode_number(filename)
    assert detection.supplementary_number is None
    if 'Ver.TV' in filename:
        assert not detection.is_standard
    state = supplementary_ordinal(video(graph(), filename))
    assert state is None or state.number is None


def test_new_tv_evidence_keeps_manual_number_and_classification_authority():
    item = video(graph(), 'Nande - NCOP Ver.TV4.mp4', episode_number_manual_override=2)
    assert supplementary_ordinal(item).number == 2
    assert effective_video_numbering(item).supplementary_number == 2
    item.content_type_manual = 'special'
    assert supplementary_ordinal(item).supplementary_type == 'special'
    assert supplementary_ordinal(item).number == 2
    item.episode_number_manual_override = None
    assert supplementary_ordinal(item).number is None


def test_nande_existing_confirmed_lanes_resolve_four_ordinals_without_writes():
    title = graph()
    items = [video(title, f'Show - NCOP{n}.mp4', n, video_variant_group_id=8) for n in range(1, 5)]
    items += [video(title, f'Show - NCOP Ver.TV{n}.mp4', n + 4, video_variant_group_id=9) for n in range(1, 5)]
    before = [(v.video_variant_group_id, v.episode_number_manual_override, v.media_part_number) for v in items]
    assert supplementary_inventory(items).logical_count == 4
    assert unresolved_duplicate_groups(items) == ()
    assert [(v.video_variant_group_id, v.episode_number_manual_override, v.media_part_number) for v in items] == before
    for v in items:
        v.video_variant_group_id = None
    assert supplementary_inventory(items).logical_count is None
    assert len(unresolved_duplicate_groups(items)) == 4
    assert all(v.video_variant_group_id is None for v in items)


@pytest.mark.parametrize('filenames,subtype,number', [
    (('Show NCED 02a.mkv', 'Show NCED 02b.mkv'), 'nced', 2),
    (('Show - Special 02.mkv', 'show_bd_spec_02.mp4'), 'special', 2),
])
def test_newly_recognized_ordinal_does_not_silently_merge_copies(filenames, subtype, number):
    title = graph('bonus')
    items = [video(title, name, i) for i, name in enumerate(filenames, 1)]
    assert all(supplementary_ordinal(v).number == number for v in items)
    assert supplementary_inventory(items).logical_count is None
    groups = unresolved_duplicate_groups(items)
    assert len(groups) == 1 and groups[0].supplementary_type == subtype
    assert all(v.video_variant_group_id is None and v.duplicate_of_video_id is None for v in items)


def test_tv_ordinal_and_manual_media_parts_remain_separate():
    title = graph()
    items = [video(title, 'Show NCOP Ver.TV1.mkv', p, media_part_number=p)
             for p in (1, 2)]
    assert supplementary_inventory(items).logical_count == 1
    assert [supplementary_ordinal(v).number for v in items] == [1, 1]
    assert [v.media_part_number for v in items] == [1, 2]
