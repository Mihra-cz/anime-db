"""Naming work is a derived text decision, separate from catalog authority."""
import importlib
import importlib.util
from dataclasses import replace
from decimal import Decimal

import pytest
from sqlalchemy import event

from test_physical_naming import session, collection, title, attach_metadata, NOW
from app.models import Video
from app.physical_naming_service import confirm_physical_naming_choice


def review_api():
    assert importlib.util.find_spec('app.naming_review') is not None, 'Physical Naming review model is missing'
    assert importlib.util.find_spec('app.naming_review_service') is not None, 'Physical Naming review service is missing'
    return importlib.import_module('app.naming_review'), importlib.import_module('app.naming_review_service')


def index(session):
    api, service = review_api()
    return api.build_naming_review(service.load_naming_review_context(session))


def test_safe_defaults_and_inheritance_are_not_work(session):
    root=collection(session)
    main=title(session,root,'Show')
    main.metadata_record.title_romaji='Show'
    child=title(session,root,'NCOP',kind='bonus',metadata=False)
    session.commit()
    result=index(session)
    assert not result.pending
    assert result.collection_badges[root.id].label=='Pojmenování OK'
    assert result.units['title',child.id].dependency==('title',main.id)
    assert not result.units['title',child.id].actionable
    assert not session.new and not session.dirty and not session.deleted


@pytest.mark.parametrize('romaji,current,reason', [('a'*71,'a'*71,'readability'),('a'*40,'b'*30,'shorter_current'),('a'*20,'b'*14,'shorter_current')])
def test_contract_readability_and_shorter_current(session,romaji,current,reason):
    root=collection(session,romaji)
    main=title(session,root,current)
    main.metadata_record.title_romaji=romaji
    session.commit()
    unit=index(session).units['title',main.id]
    assert reason in unit.reasons and unit.actionable
    assert next(c for c in unit.candidates if c.kind=='romaji').can_confirm
    confirm_physical_naming_choice(session,main,romaji,'romaji',now=NOW)
    session.commit()
    assert not index(session).units['title',main.id].actionable


def test_title_current_is_omitted_for_technical_label_not_guessed_from_filename(session):
    root=collection(session)
    main=title(session,root,'Season 1')
    session.add(Video(catalog_title=main,catalog_collection=root,filename='Short - 01.mkv',relative_path='Show/01.mkv',root_folder='Show',size=1,mtime_ns=1,season_episode_number=1))
    session.commit()
    unit=index(session).units['title',main.id]
    assert not any(c.kind=='current' for c in unit.candidates)
    assert 'shorter_current' not in unit.reasons


def test_candidates_use_confirmed_payload_and_stable_synonym_keys(session):
    root=collection(session)
    main=title(session,root,'Current Name')
    main.metadata_record.synonyms_json='["One", "Two"]'
    session.commit()
    first=index(session).units['title',main.id]
    assert {c.kind for c in first.candidates}=={'romaji','english','synonym','current'}
    old={c.raw_text:c.key for c in first.candidates if c.kind=='synonym'}
    main.metadata_record.synonyms_json='["Two", "One"]'
    session.commit()
    second=index(session).units['title',main.id]
    assert old=={c.raw_text:c.key for c in second.candidates if c.kind=='synonym'}
    assert first.fingerprint==second.fingerprint
    main.metadata_record.metadata_external_id='mismatched'
    session.commit()
    assert not any(c.kind in {'romaji','english','synonym'} for c in index(session).units['title',main.id].candidates)


def test_ambiguous_root_has_labeled_sources_and_no_automatic_winner(session):
    root=collection(session,'Monogatari (J23)')
    a=title(session,root,'First',season=2)
    b=title(session,root,'Second',season=3)
    a.metadata_record.title_romaji='First Romaji'
    b.metadata_record.title_romaji='Second Romaji'
    session.commit()
    unit=index(session).units['collection',root.id]
    assert unit.resolution.authority=='ambiguous' and unit.resolution.default_candidate is None
    assert 'authority_ambiguous' in unit.reasons
    assert {c.source_title_id for c in unit.candidates if c.kind=='romaji'}=={a.id,b.id}
    assert next(c for c in unit.candidates if c.kind=='current').raw_text=='Monogatari'
    assert not any(c.selected for c in unit.candidates)


def test_inherited_children_attach_to_parent_work_without_duplicating_decisions(session):
    root=collection(session,'a'*71)
    main=title(session,root,'a'*71)
    main.metadata_record.title_romaji='a'*71
    child=title(session,root,'NCOP',kind='bonus',metadata=False)
    session.commit()
    result=index(session)
    parent=result.units['title',main.id]
    assert ('title',child.id) in parent.dependents
    assert ('title',child.id) not in {u.owner_key for u in result.pending}
    assert result.title_badges[child.id].state=='review'


@pytest.mark.parametrize('season',[2,4])
def test_split_season_offers_effective_parent_snapshots(session,season):
    root=collection(session)
    p1=title(session,root,'P1',season=season,part=1,kind='part')
    p2=title(session,root,'P2',season=season,part=2,kind='part')
    child=title(session,root,'NCOP',season=season,part=None,kind='bonus',metadata=False)
    confirm_physical_naming_choice(session,p1,'Short P1','custom',now=NOW)
    session.commit()
    unit=index(session).units['title',child.id]
    candidates=[c for c in unit.candidates if c.kind=='parent_prefix']
    assert {c.source_title_id for c in candidates}=={p1.id,p2.id}
    assert next(c for c in candidates if c.source_title_id==p1.id).raw_text=='Short P1'
    assert 'season_only_multiple_parts' in unit.reasons


def test_invalid_alternative_does_not_break_safe_effective_default(session):
    root=collection(session,'Show')
    main=title(session,root,'Show')
    main.metadata_record.title_romaji='Show'
    main.metadata_record.title_english='あ'*86
    session.commit()
    result=index(session)
    assert result.title_badges[main.id].state=='ok'
    assert not next(c for c in result.units['title',main.id].candidates if c.kind=='english').can_confirm


def test_known_final_filename_limit_includes_suffix_and_inherited_children(session):
    root=collection(session,'Show')
    main=title(session,root,'a'*243)
    main.metadata_record.title_romaji='a'*243
    child=title(session,root,'OVA',kind='ova',metadata=False)
    session.add(Video(catalog_title=child,catalog_collection=root,filename='Show.OVA.MKV',relative_path='Show/OVA.mkv',root_folder='Show',size=1,mtime_ns=1,content_type_manual='ova',episode_number_manual_override=1,media_part_number=1))
    session.commit()
    result=index(session)
    unit=result.units['title',main.id]
    romaji=next(c for c in unit.candidates if c.kind=='romaji')
    assert romaji.sanitized.valid and not romaji.can_confirm
    assert romaji.max_component_bytes>255
    assert result.title_badges[main.id].state=='problem'
    assert ('title',child.id) not in {u.owner_key for u in result.pending}


def test_preloaded_review_render_is_sql_free_and_future_planner_messages_are_explicit(session):
    root=collection(session)
    title(session,root)
    session.commit()
    api,service=review_api()
    context=service.load_naming_review_context(session)
    def deny(*args): raise AssertionError('Pure review cannot execute SQL')
    event.listen(session.bind,'before_cursor_execute',deny)
    try:
        a=api.build_naming_review(context)
        assert a==api.build_naming_review(context)
        assert all(not u.planner_warnings for u in a.units.values())
        warning=api.PlannerNamingWarning('windows_path_budget',actual_units=241,target_units=240,overflow_units=1)
        updated=api.build_naming_review(context,planner_warnings={('collection',root.id):(warning,)})
        unit=updated.units['collection',root.id]
        assert unit.actionable and unit.planner_warnings==(warning,)
    finally: event.remove(session.bind,'before_cursor_execute',deny)


def test_custom_candidate_defensively_rejects_surrogates_without_crashing(session):
    root=collection(session)
    main=title(session,root)
    session.commit()
    api,service=review_api()
    context=service.load_naming_review_context(session)
    candidate=api.make_naming_candidate(context,('title',main.id),'\ud800','custom')
    assert not candidate.can_confirm
    assert any(d.code=='invalid_logical_input' for d in candidate.diagnostics)


def test_confirmed_english_and_synonyms_remain_available_when_romaji_is_missing(session):
    root=collection(session)
    main=title(session,root)
    main.metadata_record.title_romaji=None
    session.commit()
    unit=index(session).units['title',main.id]
    assert unit.actionable and unit.resolution.default_candidate is None
    assert {'english','synonym'} <= {c.kind for c in unit.candidates}


@pytest.mark.parametrize('season,episode,filename', [(0,1,'Show.mkv'),(1,0,'Show.mkv'),(1,1,'Show')])
def test_unresolved_filename_identity_is_deferred_not_a_bad_human_name(session,season,episode,filename):
    root=collection(session,'Show')
    main=title(session,root,'Show',season=season)
    main.metadata_record.title_romaji='Show'
    session.add(Video(catalog_title=main,catalog_collection=root,filename=filename,relative_path='Show/'+filename,
                      root_folder='Show',size=1,mtime_ns=1,content_type_manual='episode',season_episode_number=episode))
    session.commit()
    unit=index(session).units['title',main.id]
    assert next(c for c in unit.candidates if c.kind=='romaji').can_confirm
    assert unit.status=='ok' and unit.deferred_file_count==1


def test_review_evidence_traversal_is_linear_in_owners_not_candidates(session):
    api,service=review_api()
    class CountingEvidence(dict):
        visits=0
        def items(self):
            self.visits+=len(self)
            return super().items()
    for n in range(30):
        root=collection(session,f'Root {n}')
        main=title(session,root,f'Title {n}')
        main.metadata_record.synonyms_json='["Alias1", "Alias2", "Alias3", "Alias4"]'
    session.commit()
    context=service.load_naming_review_context(session)
    evidence=CountingEvidence(context.titles)
    result=api.build_naming_review(replace(context,titles=evidence))
    assert len(result.units)==60
    assert evidence.visits<=2*len(evidence)
