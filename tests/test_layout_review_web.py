"""Layout review is explicit grouping authority over a batch read model."""
import importlib
import importlib.util
import re

import pytest
from sqlalchemy import event, select

from app.config import Settings
from app.database import Base
from app.main import create_app
from app.models import CatalogTitle, PhysicalLayoutChoice, VideoVariantGroup
from app.physical_layout_service import confirm_physical_layout_choice
from app.physical_naming_service import confirm_physical_naming_choice
from test_naming_review_web import EndpointClient
from test_physical_naming import NOW, attach_metadata, collection, title
from test_physical_layout import add_video, domain_snapshot


def api():
    assert importlib.util.find_spec('app.layout_review_service'), 'Layout Review presentation foundation is missing'
    return importlib.import_module('app.layout_review'), importlib.import_module('app.layout_review_service')


@pytest.fixture
def web(tmp_path):
    app = create_app(Settings(anime_path=tmp_path/'media', database_url=f"sqlite:///{tmp_path/'layout.db'}", metadata_artwork_directory=tmp_path/'artwork', metadata_download_artwork=False))
    Base.metadata.create_all(app.state.sessions.kw['bind'])
    with app.state.sessions() as session:
        root = collection(session, 'Kobayashi / Library')
        title(session, root, 'Main', season=1)
        title(session, root, 'Main S2', season=2)
        mini = title(session, root, 'Mini Dra', season=2, kind='bonus', metadata=False)
        attach_metadata(mini, '132096', 'Kobayashi-san Chi no Maidragon S: Mini Dra')
        for n in range(1, 14): add_video(session, mini, n)
        ova = title(session, root, 'Oresuki', kind='ova', metadata=False)
        attach_metadata(ova, '114195', 'Oresuki: Oretachi no Game Set')
        add_video(session, ova)
        mp = title(session, root, 'Arifureta MP', kind='ova')
        for n in (1, 2): add_video(session, mp, 1, media_part_number=n)
        interview = title(session, root, 'Isekai Maou Interviews', season=None, kind='special', metadata=False)
        for n in (1, 2): add_video(session, interview, n, filename=f'[IV{n:02d}].mkv')
        shared = title(session, root, 'Shared Special', kind='special', metadata=False)
        add_video(session, shared)
        preview = title(session, root, 'Initium Iter', kind='preview')
        add_video(session, preview)
        root_ova = title(session, root, 'Root OVA', season=None, kind='ova')
        add_video(session, root_ova)
        session.commit()
        ids = dict(root=root.id, mini=mini.id, ova=ova.id, mp=mp.id, interview=interview.id, shared=shared.id, preview=preview.id, root_ova=root_ova.id)
    return app, EndpointClient(app), ids


def index(app):
    model, service = api()
    with app.state.sessions() as session:
        result = model.build_layout_review(service.load_layout_review_context(session))
        assert (len(session.new), len(session.dirty), len(session.deleted)) == (0, 0, 0)
        return result


def submit(web, key, kind='', action='save', fingerprint=None, **extra):
    app, client, ids = web
    row = index(app).units[ids[key]]
    return client.post(f'/naming-review/layout/title/{row.title_id}/{action}', data={
        'fingerprint': fingerprint or row.fingerprint, 'candidate_key': kind,
        'return_to': '/naming-review/layout?status=all', **extra,
    })


def snapshot(app):
    with app.state.sessions() as session: return domain_snapshot(session)


def card(page, id):
    return re.search(rf'<article\b[^>]*data-layout-title="{id}".*?</article>', page, re.S).group()


def test_route_tabs_default_queue_filters_and_safe_defaults(web):
    app, client, ids = web
    page = client.get('/naming-review/layout')
    assert page.status_code == 200, 'Layout Review route is missing'
    for label in ('Názvy', 'Rozložení', 'K vyřízení', 'Vlastní složka', 'Sdílené', 'Potvrzené', 'Vše'): assert label in page.text
    assert 'href="/naming-review/layout"' in client.get('/naming-review').text
    for key in ('mini', 'ova', 'mp', 'interview', 'root_ova'): assert f'data-layout-title="{ids[key]}"' in page.text
    for key in ('shared', 'preview'): assert f'data-layout-title="{ids[key]}"' not in page.text
    all_page = client.get('/naming-review/layout?status=all').text
    assert 'Odvozené' in card(all_page, ids['shared'])
    assert 'Přímo v Season' in card(all_page, ids['preview'])
    assert 'checked' not in card(page.text, ids['ova'])


def test_mini_oresuki_mp_and_interview_candidates(web):
    app, client, ids = web
    rows = index(app).units
    mini, ova, mp, interview = (rows[ids[k]] for k in ('mini', 'ova', 'mp', 'interview'))
    assert mini.resolution.review_class == 'OWN_FOLDER_STRONG' and mini.resolution.logical_count == 13
    own = next(c for c in mini.candidates if c.layout_kind == 'own_folder')
    assert own.recommended and own.folder_preview == 'Kobayashi-san Chi no Maidragon S - Mini Dra'
    assert own.attachment_preview == 'Season 02'
    assert next(c for c in ova.candidates if c.layout_kind == 'shared_ova').recommended
    assert mp.resolution.logical_count == 1 and mp.physical_count == 2
    assert mp.resolution.review_class == 'OWN_FOLDER_OPTIONAL'
    assert {c.layout_kind for c in interview.candidates} == {'shared_specials', 'extras_bonus'}
    assert next(c for c in interview.candidates if c.layout_kind == 'extras_bonus').recommended


@pytest.mark.parametrize('content_types,kind,label', [
    (('cm',), 'extras_promo', 'Extras/CM/'),
    (('menu',), 'extras_menus', 'Extras/Menu/'),
    (('preview',), 'extras_promo', 'Extras/Promo/'),
    (('preview', 'cm'), 'extras_promo', 'Extras/Promo/ + Extras/CM/'),
])
@pytest.mark.parametrize('confirmed', [False, True])
def test_layout_folder_presentation_matches_content_without_changing_kinds(web, content_types, kind, label, confirmed):
    app, client, ids = web
    with app.state.sessions() as session:
        root = session.get(CatalogTitle, ids['mini']).collection
        owner = title(session, root, 'Folder presentation', kind='bonus', metadata=False)
        for content_type in content_types:
            add_video(session, owner, 1, kind=content_type)
        if confirmed:
            confirm_physical_layout_choice(session, owner, kind, now=NOW)
        session.commit()
        owner_id = owner.id
    before = snapshot(app)
    row = index(app).units[owner_id]
    candidate = next(c for c in row.candidates if c.layout_kind == kind)
    assert row.resolution.effective_layout_kind == kind
    assert row.layout_label == candidate.label == label
    assert candidate.preview == 'Season 01/\n└── ' + label
    if confirmed:
        assert row.resolution.choice.layout_kind == kind
    response = client.get(f'/naming-review/layout?status=all&title_id={owner_id}')
    assert response.status_code == 200
    output = card(response.text, owner_id)
    assert f'Aktuální rozložení: <strong>{label}</strong>' in output
    assert f'value="{kind}"' in output
    assert 'Extras/Menus/' not in output
    if content_types in {('cm',), ('menu',)}:
        assert 'Extras/Promo/' not in output
    assert label in output
    assert snapshot(app) == before


@pytest.mark.parametrize('key,kind', [('mini','own_folder'), ('ova','shared_ova'), ('interview','extras_bonus'), ('shared','shared_specials')])
def test_save_explicit_choice_prg_and_domain_isolation(web, key, kind):
    app, client, ids = web
    before = snapshot(app)
    response = submit(web, key, kind)
    assert response.status_code == 303 and response.headers['location'] == '/naming-review/layout?status=all'
    assert snapshot(app) == before
    with app.state.sessions() as session:
        choice = session.scalar(select(PhysicalLayoutChoice).where(PhysicalLayoutChoice.catalog_title_id == ids[key]))
        assert choice.layout_kind == kind
        if key == 'interview': assert session.get(CatalogTitle, ids[key]).effective_part_type == 'special'
    row = index(app).units[ids[key]]
    assert row.resolution.basis_matches and not row.actionable
    assert 'Potvrzené člověkem' in card(client.get('/naming-review/layout?status=confirmed').text, ids[key])


@pytest.mark.parametrize('key,kind', [('shared','own_folder'), ('root_ova','direct_season'), ('ova','extras_menus'), ('ova','interviews')])
def test_server_rejects_inapplicable_or_arbitrary_candidate(web, key, kind):
    before = snapshot(web[0])
    assert submit(web, key, kind).status_code == 400
    assert snapshot(web[0]) == before
    assert not index(web[0]).context.physical.choices


@pytest.mark.parametrize('change', ['attachment', 'metadata', 'content', 'bucket', 'choice'])
def test_stale_form_rejected_without_write(web, change):
    app, client, ids = web
    old = index(app).units[ids['ova']].fingerprint
    with app.state.sessions() as session:
        owner = session.get(CatalogTitle, ids['ova'])
        if change == 'attachment': owner.season_number_manual = 2
        elif change == 'metadata':
            owner.external_links[0].external_id = 'new-id'
            owner.metadata_record.metadata_external_id = 'new-id'
        elif change == 'content': owner.videos[0].content_type_manual = 'special'
        elif change == 'bucket': add_video(session, owner, 2)
        else: confirm_physical_layout_choice(session, owner, 'shared_ova')
        session.commit()
    before = snapshot(app)
    assert submit(web, 'ova', 'shared_ova', fingerprint=old).status_code == 409
    assert snapshot(app) == before


def test_reconfirm_basis_mismatch_and_reset(web):
    app, client, ids = web
    assert submit(web, 'ova', 'shared_ova').status_code == 303
    with app.state.sessions() as session:
        session.get(CatalogTitle, ids['ova']).season_number_manual = 2
        session.commit()
    assert 'Kontext fyzického rozložení se od posledního potvrzení změnil.' in client.get('/naming-review/layout').text
    assert submit(web, 'ova', action='reconfirm').status_code == 303
    assert index(app).units[ids['ova']].resolution.basis_matches
    assert submit(web, 'ova', action='reset').status_code == 303
    assert index(app).units[ids['ova']].actionable


def test_naming_rename_and_volatile_metadata_do_not_stale_layout_form(web):
    app, client, ids = web
    assert submit(web, 'mini', 'own_folder').status_code == 303
    old = index(app).units[ids['mini']].fingerprint
    with app.state.sessions() as session:
        owner = session.get(CatalogTitle, ids['mini'])
        confirm_physical_naming_choice(session, owner, 'Mini Dra: Human Short Name', 'custom')
        owner.metadata_record.title_romaji = 'Refreshed text'
        owner.metadata_record.episode_count = 16
        session.commit()
    row = index(app).units[ids['mini']]
    assert row.fingerprint == old and row.resolution.basis_matches
    assert 'Mini Dra - Human Short Name/' in client.get('/naming-review/layout?status=all').text
    assert submit(web, 'mini', 'own_folder', fingerprint=old).status_code == 303


def test_naming_dependency_fails_safe_and_xss_is_escaped(web):
    app, client, ids = web
    with app.state.sessions() as session:
        confirm_physical_naming_choice(session, session.get(CatalogTitle, ids['mini']), '<script>alert(1)</script>: Mini', 'custom')
        session.commit()
    page = client.get('/naming-review/layout').text
    assert '<script>alert(1)</script>' not in page and '&lt;script&gt;alert(1)&lt;/script&gt;' in page
    with app.state.sessions() as session:
        owner = session.get(CatalogTitle, ids['mini'])
        owner.external_links[0].external_id = 'relinked'
        owner.metadata_record.metadata_external_id = 'relinked'
        session.commit()
    row = index(app).units[ids['mini']]
    own = next(c for c in row.candidates if c.layout_kind == 'own_folder')
    assert not own.valid and own.folder_preview is None
    assert 'Názvy' in client.get('/naming-review/layout').text
    assert submit(web, 'mini', 'own_folder').status_code == 400


def test_unknown_duplicate_count_does_not_recommend_strong_or_optional(web):
    app, client, ids = web
    with app.state.sessions() as session:
        owner = session.get(CatalogTitle, ids['ova'])
        add_video(session, owner, filename='Unresolved.mkv')
        session.commit()
    row = index(app).units[ids['ova']]
    assert row.resolution.logical_count is None and row.resolution.review_class == 'NEEDS_HUMAN_LAYOUT'
    assert not any(c.recommended for c in row.candidates)
    page = client.get('/naming-review/layout').text
    assert 'Počet logických položek nelze bezpečně určit' in page
    assert 'Logickou identitu nelze bezpečně určit.' in page


def test_variants_and_confirmed_duplicates_do_not_inflate_items(web):
    app, client, ids = web
    with app.state.sessions() as session:
        owner = session.get(CatalogTitle, ids['ova'])
        first = owner.videos[0]
        first.episode_number_manual_override = 1
        first.video_variant_group = VideoVariantGroup(catalog_title=owner, manual_label='BD', verified_at=NOW)
        variant = add_video(session, owner, 1)
        variant.video_variant_group = VideoVariantGroup(catalog_title=owner, manual_label='TV', verified_at=NOW)
        secondary = add_video(session, owner, 1)
        secondary.duplicate_of = first
        session.commit()
    row = index(app).units[ids['ova']]
    assert row.resolution.logical_count == 1 and row.physical_count == 3
    assert row.resolution.review_class == 'OWN_FOLDER_OPTIONAL'


def test_filters_search_and_scoped_badge_queue_parity(web):
    app, client, ids = web
    for status in ('pending','own','shared','confirmed','all'):
        assert client.get(f'/naming-review/layout?status={status}').status_code == 200
    for q in ('Kobayashi', 'Oretachi', 'Mini Dra'):
        assert 'data-layout-title=' in client.get(f'/naming-review/layout?q={q}').text
    assert 'data-layout-title=' not in client.get('/naming-review/layout?q=no-match').text
    for path in ('/', f"/collections/{ids['root']}", f"/titles/{ids['mini']}"):
        page = client.get(path)
        assert page.status_code == 200
        links = re.findall(r'href="([^"]*naming-review/layout[^"]*)"', page.text)
        assert links, path
        import html
        assert 'data-layout-title=' in client.get(html.unescape(links[0])).text


def test_gets_are_readonly_and_query_count_bounded(web):
    app, client, ids = web
    engine = app.state.sessions.kw['bind']
    def queries(path):
        sql = []
        def record(c, cursor, statement, p, context, many): sql.append(statement)
        event.listen(engine, 'before_cursor_execute', record)
        try: assert client.get(path).status_code == 200
        finally: event.remove(engine, 'before_cursor_execute', record)
        assert all(s.lstrip().upper().startswith('SELECT') for s in sql)
        return len(sql)
    for path in ('/naming-review', '/naming-review/layout', '/naming-review/layout?status=all&q=Mini', f"/naming-review/layout?collection_id={ids['root']}", f"/naming-review/layout?title_id={ids['mini']}"):
        assert queries(path) <= 6
    baseline = queries('/naming-review/layout?status=all')
    with app.state.sessions() as session:
        for n in range(20):
            root = collection(session, f'Other {n}')
            main = title(session, root, 'Main')
            ova = title(session, root, 'OVA', kind='ova')
            add_video(session, ova)
        session.commit()
    assert queries('/naming-review/layout?status=all') == baseline
    for path in ('/', f"/collections/{ids['root']}", f"/titles/{ids['mini']}"):
        queries(path)


@pytest.mark.parametrize('headers', [{'origin': 'https://evil.example'}, {'sec-fetch-site': 'cross-site'}])
def test_cross_origin_layout_post_is_rejected(web, headers):
    app, client, ids = web
    row = index(app).units[ids['ova']]
    response = client.post(f"/naming-review/layout/title/{ids['ova']}/save", data={'fingerprint': row.fingerprint, 'candidate_key': 'shared_ova'}, headers=headers)
    assert response.status_code == 403
    assert not index(app).context.physical.choices


def test_injected_fields_and_duplicate_candidate_are_rejected(web):
    app, client, ids = web
    row = index(app).units[ids['ova']]
    path = f"/naming-review/layout/title/{ids['ova']}/save"
    for data in ({'fingerprint': row.fingerprint, 'candidate_key': 'shared_ova', 'season_number': '9'},
                 [('fingerprint', row.fingerprint), ('candidate_key', 'shared_ova'), ('candidate_key', 'own_folder')]):
        assert client.post(path, data=data).status_code == 400
    assert not index(app).context.physical.choices


def test_main_title_and_missing_owner_cannot_receive_layout_choice(web):
    app, client, ids = web
    with app.state.sessions() as session:
        main_id = session.scalar(select(CatalogTitle.id).where(CatalogTitle.local_title == 'Main'))
    assert client.post(f'/naming-review/layout/title/{main_id}/save', data={'fingerprint': 'fake', 'candidate_key': 'shared_ova'}).status_code == 400
    assert client.post('/naming-review/layout/title/999999/save', data={'fingerprint': 'fake'}).status_code == 404
    assert not index(app).context.physical.choices


def test_direct_season_reconfirm_is_revalidated_after_move_to_root(web):
    app, client, ids = web
    assert submit(web, 'preview', 'direct_season').status_code == 303
    with app.state.sessions() as session:
        session.get(CatalogTitle, ids['preview']).season_number_manual = None
        session.commit()
    assert submit(web, 'preview', action='reconfirm').status_code == 400
    with app.state.sessions() as session:
        assert session.get(CatalogTitle, ids['preview']).physical_layout_choice.layout_kind == 'direct_season'


def test_part_is_not_a_directory_and_recaps_inside_main_have_no_owner(web):
    app, client, ids = web
    with app.state.sessions() as session:
        root = session.get(CatalogTitle, ids['mini']).collection
        for n in (1, 2): title(session, root, f'Main Part {n}', season=2, part=n, kind='part')
        main = session.scalar(select(CatalogTitle).where(CatalogTitle.local_title == 'Main'))
        add_video(session, main, kind='recap', filename='Recap 5.5.mkv')
        session.commit()
        main_id = main.id
    result = index(app)
    assert main_id not in result.units
    row = result.units[ids['mini']]
    assert all(c.attachment_preview == 'Season 02' and 'Part ' not in (c.preview or '') for c in row.candidates)


def test_naming_problem_does_not_turn_confirmed_layout_into_naming_reconfirm(web):
    app, client, ids = web
    assert submit(web, 'mini', 'own_folder').status_code == 303
    with app.state.sessions() as session:
        confirm_physical_naming_choice(session, session.get(CatalogTitle, ids['mini']), 'a'*256, 'custom')
        session.commit()
    row = index(app).units[ids['mini']]
    assert row.resolution.basis_matches and not row.actionable and row.naming_warning
    assert next(c for c in row.candidates if c.layout_kind == 'own_folder').folder_preview is None


def test_unattached_story_preview_does_not_offer_promo_as_inferred_layout(web):
    app, client, ids = web
    with app.state.sessions() as session:
        session.get(CatalogTitle, ids['preview']).season_number_manual = None
        session.commit()
    row = index(app).units[ids['preview']]
    assert row.actionable
    assert {c.layout_kind for c in row.candidates} == {'own_folder'}


def test_shared_layout_does_not_request_unrelated_own_folder_naming_review(web):
    app, client, ids = web
    with app.state.sessions() as session:
        main = session.scalar(select(CatalogTitle).where(CatalogTitle.local_title == 'Main'))
        confirm_physical_naming_choice(session, main, 'Saved main name', 'custom')
        session.commit()
        main.external_links[0].external_id = 'relinked-main'
        main.metadata_record.metadata_external_id = 'relinked-main'
        session.commit()
    row = index(app).units[ids['shared']]
    assert not row.actionable and not row.naming_warning
    assert 'Náhled vlastní složky může vyžadovat' not in card(client.get('/naming-review/layout?status=all').text, ids['shared'])


def test_production_shaped_32_decisions_are_derived_not_video_count(web):
    app, client, ids = web
    with app.state.sessions() as session:
        # Fifteen coherent works, sixteen singleton identities, one interview;
        # safe shared/direct works contribute no human tasks.
        for owner in list(session.scalars(select(CatalogTitle))): session.delete(owner)
        session.flush()
        root = collection(session, 'Production shape')
        for season in (1, 2, 4): title(session, root, f'Main {season}', season=season)
        for n in range(15):
            owner = title(session, root, f'Strong {n}', kind='bonus')
            for ordinal in range(1, 14 if n == 0 else 3): add_video(session, owner, ordinal)
        for n in range(16):
            owner = title(session, root, f'Optional {n}', kind='ova')
            add_video(session, owner)
        owner = title(session, root, 'Interviews', season=None, kind='special', metadata=False)
        add_video(session, owner, 1, filename='IV01.mkv')
        for n in range(21):
            owner = title(session, root, f'Shared {n}', kind='bonus', metadata=False)
            add_video(session, owner, 1, kind='op')
        for n in range(4):
            owner = title(session, root, f'Prologue {n}', kind='preview', season=4 if n == 3 else 1, metadata=n != 3)
            add_video(session, owner)
        session.commit()
    result = index(app)
    assert len(result.units) == 57 and len(result.pending) == 32
    assert result.counts.strong == 15 and result.counts.optional == 16 and result.counts.ambiguity == 1
    assert result.counts.direct_season == 4 and result.counts.derived == 25
    assert client.get('/naming-review/layout').text.count('data-layout-title=') == 32


def test_reconfirm_is_offered_only_while_the_saved_kind_is_still_applicable(web):
    app, client, ids = web
    for key, kind in (('ova', 'shared_ova'), ('preview', 'direct_season')):
        assert submit(web, key, kind).status_code == 303
    with app.state.sessions() as session:
        session.get(CatalogTitle, ids['ova']).season_number_manual = 2
        session.get(CatalogTitle, ids['preview']).season_number_manual = None
        session.commit()
    page = client.get('/naming-review/layout').text
    assert '/reconfirm' in card(page, ids['ova'])
    # Direct Season cannot apply at root; the server would reject reconfirm,
    # so the card must not offer it. Reset stays available.
    stale_root = card(page, ids['preview'])
    assert '/reconfirm' not in stale_root and '/reset' in stale_root


def test_item_without_any_applicable_layout_points_to_hierarchy(web):
    app, client, ids = web
    with app.state.sessions() as session:
        root = session.get(CatalogTitle, ids['mini']).collection
        other = title(session, root, 'Other extras', kind='other', metadata=False)
        add_video(session, other, 1, kind='other', filename='Something 01.mkv')
        automatic = title(session, root, 'Automatic Preview', kind='preview', metadata=False)
        automatic.hierarchy_manual_override = False
        automatic.part_type_manual = automatic.season_number_manual = automatic.hierarchy_verified_at = None
        add_video(session, automatic, filename='Preview.mkv')
        session.commit()
        owners = (other.id, automatic.id)
    page = client.get('/naming-review/layout').text
    for owner_id in owners:
        row = index(app).units[owner_id]
        assert row.actionable and not row.candidates
        assert any('Hierarchi' in reason for reason in row.reason_messages)
        assert 'Evidence připouští více seskupení' not in card(page, owner_id)


def test_issue_279_production_shape_saves_extras_bonus_and_keeps_special(web):
    from app.catalog import effective_video_content_type
    app, client, ids = web
    with app.state.sessions() as session:
        root = collection(session, 'Isekai Maou to Shoukan Shoujo no Dorei Majutsu')
        owner = title(session, root, 'Interview - Isekai Maou to Shoukan Shoujo no Dorei Majutsu',
                      season=None, kind='special', metadata=False)
        for n in (1, 2):
            add_video(session, owner, n, kind='other', filename=(
                f'[Anipakku] Isekai Maou to Shoukan Shoujo no Dorei Majutsu [IV{n:02d}][Ma10p_1080p][x265_aac].mkv'))
        session.commit()
        ids['issue_279'] = owner.id
    def layout_tab_count():
        page = client.get('/naming-review/layout').text
        return int(re.search(r'Rozložení <span class="naming-filter-count">(\d+) k vyřízení', page).group(1))
    before_count = layout_tab_count()
    row = index(app).units[ids['issue_279']]
    assert row.actionable and row.resolution.logical_count == 2
    assert [(c.label, c.recommended) for c in row.candidates] == [('Specials/', False), ('Extras/Bonus/', True)]
    page = card(client.get('/naming-review/layout').text, ids['issue_279'])
    assert 'Extras/Bonus/' in page and 'Interview' not in page.split('</h2>', 1)[1]
    assert submit(web, 'issue_279', 'extras_bonus').status_code == 303
    with app.state.sessions() as session:
        owner = session.get(CatalogTitle, ids['issue_279'])
        assert owner.physical_layout_choice.layout_kind == 'extras_bonus'
        assert owner.effective_part_type == 'special' and owner.part_type_manual == 'special'
        assert all(video.content_type_manual is None for video in owner.videos)
        assert {effective_video_content_type(video, owner) for video in owner.videos} == {'special'}
    assert layout_tab_count() == before_count - 1


def test_filename_size_and_mtime_changes_do_not_stale_layout_form(web):
    app, client, ids = web
    old = index(app).units[ids['ova']].fingerprint
    with app.state.sessions() as session:
        video = session.get(CatalogTitle, ids['ova']).videos[0]
        video.filename, video.size, video.mtime_ns = 'Oresuki BD - OVA 01.mkv', 999, 123456789
        session.commit()
    assert index(app).units[ids['ova']].fingerprint == old
    assert submit(web, 'ova', 'shared_ova', fingerprint=old).status_code == 303
