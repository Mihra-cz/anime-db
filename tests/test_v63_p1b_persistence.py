"""Persisted v8 -> v9 identity, grouping, and SQLite lifecycle contracts."""
import json

import pytest
from fastapi import HTTPException
from sqlalchemy import MetaData, UniqueConstraint, event, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import Base, make_engine
from app import migrations
from app.hierarchy_review import (
    collection_grouping_authority_targets, record_manual_collection_merge,
    resolve_grouping_owner_references,
)
from app.models import CatalogCollection, CatalogTitle, CollectionGroupingDecision, ManualSplitRuleVideo, Video
from app.title_identity import TitleLocatorIndex
from test_hierarchy_rebuild import _collection, _title, _video, PROBE_RESULT


def v8_engine(tmp_path, *, locator_unique=True, autoincrement=False):
    """A real old schema, independent of current ORM uniqueness/FK definitions."""
    engine = make_engine(f"sqlite:///{tmp_path / 'old.db'}")
    metadata = MetaData()
    for table in Base.metadata.sorted_tables:
        if table.name == 'grouping_decision_titles':
            continue
        clone = table.to_metadata(metadata)
        if table.name == 'catalog_titles':
            for index in list(clone.indexes):
                if [c.name for c in index.columns] == ['relative_root_path']:
                    clone.indexes.remove(index)
            for constraint in list(clone.constraints):
                if isinstance(constraint, UniqueConstraint) and list(constraint.columns.keys()) == ['relative_root_path']:
                    clone.constraints.remove(constraint)
            clone.dialect_options['sqlite']['autoincrement'] = autoincrement
            if locator_unique:
                clone.append_constraint(UniqueConstraint('relative_root_path'))
        if table.name == 'collection_grouping_decisions' and 'target_collection_id' in clone.c:
            col = clone.c.target_collection_id
            for constraint in list(clone.constraints):
                if col.name in constraint.columns:
                    clone.constraints.remove(constraint)
            for index in list(clone.indexes):
                if col.name in index.columns:
                    clone.indexes.remove(index)
            clone._columns.remove(col)
    metadata.create_all(engine)
    with engine.begin() as c:
        c.exec_driver_sql('PRAGMA user_version=8')
    return engine


def schema(engine):
    with engine.connect() as c:
        return tuple(c.exec_driver_sql("SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name"))


def test_real_v8_unique_rejects_and_v9_persists_shared_locator(tmp_path):
    engine = v8_engine(tmp_path)
    before = inspect(engine)
    assert before.get_columns('catalog_titles')[4]['nullable'] is False
    assert any(c['column_names'] == ['relative_root_path'] for c in before.get_unique_constraints('catalog_titles'))
    with engine.connect() as c:
        assert 'UNIQUE (relative_root_path)' in c.scalar(text("SELECT sql FROM sqlite_schema WHERE name='catalog_titles'"))
    with Session(engine) as s:
        s.add(_title('Show/Season 02', _collection('Show')))
        s.commit()
        s.add(_title('Show/Season 02', s.scalar(select(CatalogCollection))))
        with pytest.raises(IntegrityError):
            s.commit()
    assert migrations.migrate_schema_at_startup(engine)
    with Session(engine) as s:
        a = s.scalar(select(CatalogTitle))
        b = _title(a.relative_root_path, a.collection)
        s.add(b)
        s.commit()
        ids = {a.id, b.id}
    with Session(engine) as s:
        rows = s.scalars(select(CatalogTitle)).all()
        assert {t.id for t in rows} == ids
        assert len(TitleLocatorIndex(rows).candidates('Show/Season 02')) == 2
        assert TitleLocatorIndex(reversed(rows)).unique('Show/Season 02') is None
    after = inspect(engine)
    assert not any(c['column_names'] == ['relative_root_path'] for c in after.get_unique_constraints('catalog_titles'))
    assert any(i['column_names'] == ['relative_root_path'] and not i['unique'] for i in after.get_indexes('catalog_titles'))
    assert {c['name']: c['nullable'] for c in after.get_columns('catalog_titles')}['relative_root_path'] is False
    assert after.get_foreign_keys('catalog_titles') == before.get_foreign_keys('catalog_titles')
    assert after.get_check_constraints('catalog_titles') == before.get_check_constraints('catalog_titles')
    with engine.connect() as c:
        assert c.scalar(text('PRAGMA user_version')) == 9
        assert c.scalar(text('PRAGMA foreign_keys')) == 1
        assert list(c.exec_driver_sql('PRAGMA foreign_key_check')) == []
        assert c.scalar(text('PRAGMA integrity_check')) == 'ok'
        assert c.exec_driver_sql('PRAGMA foreign_key_list(catalog_titles)').all()


def seed_legacy(engine):
    with engine.begin() as c:
        c.exec_driver_sql("INSERT INTO catalog_collections (id,local_title,normalized_local_title,relative_root_path,created_at,updated_at) VALUES (1,'Target','target','Target','2000-01-01 00:00:00','2000-01-01 00:00:00')")
        for n in range(18):
            c.exec_driver_sql("INSERT INTO catalog_titles (id,catalog_collection_id,local_title,normalized_local_title,relative_root_path,created_at,updated_at) VALUES (?,1,?,?,?,'2000-01-01 00:00:00','2000-01-01 00:00:00')", (n+1, f'Title {n}',f'title {n}',f'Old/{n}'))
        for n in range(13):
            paths = ([f'Old/{2*n}',f'Old/{2*n+1}'] if n < 9 else [f'Missing/{2*(n-9)}',f'Missing/{2*(n-9)+1}'] if n < 12 else [])
            c.exec_driver_sql("INSERT INTO collection_grouping_decisions (id,suggestion_key,state_fingerprint,decision,target_collection_path,selected_title_paths_json,created_at,updated_at) VALUES (?,?,?,'merged',?,?, '2000-01-01 00:00:00','2000-01-01 00:00:00')", (n+1,str(n),'legacy','Target' if n < 12 else None,json.dumps(paths)))


def test_grouping_exact_18_refs_6_missing_and_no_rebinding(tmp_path):
    engine = v8_engine(tmp_path)
    seed_legacy(engine)
    with engine.connect() as c:
        old = c.exec_driver_sql('SELECT * FROM collection_grouping_decisions ORDER BY id').all()
    migrations.migrate_schema_at_startup(engine)
    with Session(engine) as s:
        decisions = s.scalars(select(CollectionGroupingDecision)).all()
        assert len(decisions) == 13
        refs = [r for d in decisions for r in d.selected_titles]
        assert len(refs) == 24
        assert {r.catalog_title_id for r in refs if r.catalog_title_id is not None} == set(range(1,19))
        assert sorted(r.title_path_snapshot for r in refs if r.catalog_title_id is None) == [f'Missing/{n}' for n in range(6)]
        assert [d.target_collection_id for d in decisions] == [1]*12 + [None]
        assert len(collection_grouping_authority_targets(s)) == 18
        s.add(_title('Missing/0', s.get(CatalogCollection,1)))
        s.get(CatalogTitle,1).relative_root_path = 'renamed locator'
        s.get(CatalogCollection,1).relative_root_path = 'renamed target'
        s.commit()
    with Session(engine) as s:
        targets = collection_grouping_authority_targets(s)
        assert {t.id for t in targets} == set(range(1,19))
        d = s.get(CollectionGroupingDecision,10)
        result = resolve_grouping_owner_references(d, TitleLocatorIndex(s.scalars(select(CatalogTitle))), {c.id:c for c in s.scalars(select(CatalogCollection))})
        assert result.title_ids == ()
        assert result.missing_paths == ('Missing/0','Missing/1')
    with engine.connect() as c:
        cols = [x['name'] for x in inspect(c).get_columns('collection_grouping_decisions') if x['name'] != 'target_collection_id']
        assert c.exec_driver_sql('SELECT '+','.join(cols)+' FROM collection_grouping_decisions ORDER BY id').all() == old


def test_new_grouping_ids_survive_rename_delete_and_reused_paths(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path/'new.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        source, target = _collection('Source'), _collection('Target')
        title = _title('Source/title', source)
        s.add_all([title,target]); s.commit()
        decision = record_manual_collection_merge(s,target,[title.id]); s.commit()
        ids = (decision.id,title.id,target.id)
        title.relative_root_path = 'changed'
        target.relative_root_path = 'changed target'
        s.commit()
    with Session(engine) as s:
        assert {t.id:c.id for t,c in collection_grouping_authority_targets(s).items()} == {ids[1]:ids[2]}
        d=s.get(CollectionGroupingDecision,ids[0])
        assert d.target_collection_id == ids[2]
        assert [r.catalog_title_id for r in d.selected_titles] == [ids[1]]
        s.delete(s.get(CatalogTitle,ids[1])); s.commit()
    with Session(engine) as s:
        d=s.get(CollectionGroupingDecision,ids[0])
        assert d.selected_titles[0].catalog_title_id is None
        assert d.selected_titles[0].title_path_snapshot == 'Source/title'
        s.add(_title('Source/title',s.scalar(select(CatalogCollection).where(CatalogCollection.relative_root_path=='Source')))); s.commit()
        assert collection_grouping_authority_targets(s) == {}
        s.delete(s.get(CatalogCollection,ids[2])); s.commit()
    with Session(engine) as s:
        d=s.get(CollectionGroupingDecision,ids[0])
        assert d.target_collection_id is None
        assert d.target_collection_path == 'Target'
        s.add(_collection('Target')); s.commit()
        assert collection_grouping_authority_targets(s) == {}


@pytest.mark.parametrize('phase',['copy','grouping','fk','integrity'])
def test_migration_failure_is_atomic(tmp_path,monkeypatch,phase):
    engine=v8_engine(tmp_path); seed_legacy(engine)
    before=schema(engine)
    with engine.connect() as c:
        rows=c.exec_driver_sql('SELECT * FROM catalog_titles ORDER BY id').all()
    checks = {'fk': 0, 'integrity': 0}
    def inject(c,cursor,sql,params,context,many):
        upper=sql.upper()
        if upper.startswith('PRAGMA FOREIGN_KEY_CHECK'): checks['fk'] += 1
        if upper.startswith('PRAGMA INTEGRITY_CHECK'): checks['integrity'] += 1
        hit = (phase=='copy' and upper.startswith('INSERT INTO CATALOG_TITLES')) or (phase=='grouping' and upper.startswith('INSERT INTO GROUPING_DECISION_TITLES')) or (phase=='fk' and upper.startswith('PRAGMA FOREIGN_KEY_CHECK') and checks['fk']==2) or (phase=='integrity' and upper.startswith('PRAGMA INTEGRITY_CHECK') and checks['integrity']==2)
        if hit: raise RuntimeError('injected '+phase)
    event.listen(engine,'before_cursor_execute',inject)
    try:
        with pytest.raises(RuntimeError,match='injected'):
            migrations.migrate_schema_at_startup(engine)
    finally:
        event.remove(engine,'before_cursor_execute',inject)
    assert schema(engine)==before
    with engine.connect() as c:
        assert c.scalar(text('PRAGMA user_version')) == 8
        assert c.scalar(text('PRAGMA foreign_keys')) == 1
        assert c.exec_driver_sql('SELECT * FROM catalog_titles ORDER BY id').all() == rows
        assert c.exec_driver_sql('PRAGMA foreign_key_check').all() == []
        assert c.scalar(text('PRAGMA integrity_check'))=='ok'


def sqlite_fks(engine, name):
    with engine.connect() as c:
        return sorted(tuple(row[1:]) for row in c.exec_driver_sql(f'PRAGMA foreign_key_list("{name}")'))


def normalized_schema(engine):
    inspector=inspect(engine)
    return {name: (
        sorted((x['name'],str(x['type']),x['nullable'],x['default'],x['primary_key']) for x in inspector.get_columns(name)),
        sqlite_fks(engine, name),
        sorted((x['name'],tuple(x['column_names']),x['unique']) for x in inspector.get_indexes(name)),
        sorted((x['name'],x['sqltext']) for x in inspector.get_check_constraints(name)),
        sorted(tuple(x['column_names']) for x in inspector.get_unique_constraints(name)),
    ) for name in inspector.get_table_names()}


def test_second_startup_no_dml_ddl_and_create_all_schema_parity(tmp_path):
    engine=v8_engine(tmp_path); seed_legacy(engine)
    migrations.migrate_schema_at_startup(engine)
    with engine.connect() as c:
        assert c.scalar(text('PRAGMA user_version')) == 9
    before=schema(engine)
    sql=[]
    def record(c,cursor,statement,*args): sql.append(statement)
    event.listen(engine,'before_cursor_execute',record)
    try:
        assert migrations.migrate_schema_at_startup(engine) is False
        Base.metadata.create_all(engine)
    finally: event.remove(engine,'before_cursor_execute',record)
    assert not any(x.lstrip().upper().startswith(('INSERT','UPDATE','DELETE','CREATE','DROP','ALTER')) for x in sql)
    assert schema(engine)==before
    fresh=make_engine('sqlite://'); Base.metadata.create_all(fresh)
    assert 'grouping_decision_titles' in inspect(fresh).get_table_names()
    assert not any(x['column_names'] == ['relative_root_path']
                   for x in inspect(fresh).get_unique_constraints('catalog_titles'))
    assert normalized_schema(engine)==normalized_schema(fresh)


def test_legacy_url_reports_ambiguous_locator(tmp_path):
    from app.config import Settings
    from app.main import create_app
    from test_metadata_split import web_request
    app=create_app(Settings(anime_path=tmp_path,database_url=f"sqlite:///{tmp_path/'web.db'}",metadata_download_artwork=False))
    with app.state.sessions() as s:
        Base.metadata.create_all(s.get_bind())
        col=_collection('Show')
        s.add_all([_title('Show/Season 02',col),_title('Show/Season 02',col)])
        s.commit()
    endpoint=next(r.endpoint for r in app.routes if r.path=='/catalog/{filter_name}/series')
    with pytest.raises(HTTPException) as exc:
        endpoint(web_request(app,'/catalog/all/series'), 'all',series_path='Show/Season 02')
    assert exc.value.status_code==409


@pytest.fixture(params=[('ReZero',2,None),('SAO',4,'preview'),('Slime',2,'bonus')])
def persisted_shared(request,tmp_path):
    name,season,supplementary=request.param
    engine=v8_engine(tmp_path)
    migrations.migrate_schema_at_startup(engine)
    with Session(engine) as s:
        col=_collection(f'Anime/{name}')
        for part in (1,2):
            title=_title(f'Anime/{name}/Season {season:02}',col,f'Part {part}',
                part_type='season',season_number=season,part_number=part,
                hierarchy_manual_override=True,part_type_manual='season',
                season_number_manual=season,part_number_manual=part,sort_order=part)
            v=_video(f'Anime/{name}/Season {season:02}/E{part:02}.mkv',title=title,collection=col,
                file_type='episode',episode_number_manual_override=part)
            s.add(v); s.flush()
            s.add(ManualSplitRuleVideo(catalog_title=title,video=v))
        if supplementary:
            title=_title(f'Anime/{name}/Season {season:02}',col,'Reflection' if name=='SAO' else 'NC',
                part_type=supplementary,season_number=season,hierarchy_manual_override=True,
                part_type_manual=supplementary,season_number_manual=season)
            kind='preview' if name=='SAO' else 'ncop'
            v=_video(f'Anime/{name}/Season {season:02}/{kind}.mkv',title=title,collection=col,
                file_type=kind,content_type_manual=kind)
            s.add(v); s.flush(); s.add(ManualSplitRuleVideo(catalog_title=title,video=v))
        s.commit()
    yield engine
    engine.dispose()


def test_persisted_shared_owners_preview_apply_repreview_reverse_and_stale(persisted_shared,monkeypatch):
    import app.hierarchy_rebuild as rebuild
    engine=persisted_shared
    with Session(engine) as s:
        rows=s.scalars(select(CatalogTitle)).all()
        ids={t.id for t in rows}; locator=rows[0].relative_root_path
        assert len(TitleLocatorIndex(rows).candidates(locator))==len(ids)
        assert TitleLocatorIndex(rows).unique(locator) is None
        membership={v.id:v.catalog_title_id for v in s.scalars(select(Video))}
        plan=rebuild.build_hierarchy_rebuild_plan(s)
        assert {t.title_id for t in plan.titles}==ids
        original=rebuild._load_state
        def reversed_load(session):
            collections,titles,videos=original(session)
            return collections,list(reversed(titles)),list(reversed(videos))
        with monkeypatch.context() as patch:
            patch.setattr(rebuild,'_load_state',reversed_load)
            reverse = rebuild.build_hierarchy_rebuild_plan(s)
            assert reverse.source_fingerprint == plan.source_fingerprint
            assert {a.video_id:a for a in reverse.video_assignments} == {a.video_id:a for a in plan.video_assignments}
            assert {n.video_id:n for n in reverse.numbering} == {n.video_id:n for n in plan.numbering}
            assert reverse.titles == plan.titles
            assert reverse.blockers == plan.blockers
        assert not s.new and not s.dirty and not s.deleted
        assert rebuild.apply_hierarchy_rebuild_plan(s,plan).applied
        s.commit()
    with Session(engine) as s:
        again=rebuild.build_hierarchy_rebuild_plan(s)
        assert not again.has_changes
        assert {t.id for t in s.scalars(select(CatalogTitle))}==ids
        assert {v.id:v.catalog_title_id for v in s.scalars(select(Video))}==membership
        title=s.get(CatalogTitle,min(ids)); title.manual_display_title='Changed'; s.commit()
        with pytest.raises(rebuild.HierarchyPlanStaleError): rebuild.apply_hierarchy_rebuild_plan(s,again)


def test_persisted_automatic_collision_blocks_without_merge(persisted_shared):
    import app.hierarchy_rebuild as rebuild
    with Session(persisted_shared) as s:
        for t in s.scalars(select(CatalogTitle)):
            t.hierarchy_manual_override=False
            t.part_type_manual=None; t.season_number_manual=None; t.part_number_manual=None
            t.manual_split_rule_videos.clear()
        s.commit()
        plan=rebuild.build_hierarchy_rebuild_plan(s)
        assert any(b.code=='ambiguous_title_locator' and b.prevents_apply for b in plan.blockers)
        assert all(t.action!='remove' for t in plan.titles)
        with pytest.raises(rebuild.HierarchyPlanBlockedError): rebuild.apply_hierarchy_rebuild_plan(s,plan)


def test_rebuild_owner_create_update_delete_keeps_persisted_shared_owners(persisted_shared):
    import app.hierarchy_rebuild as rebuild
    with Session(persisted_shared) as s:
        owners = s.scalars(select(CatalogTitle)).all()
        owner_ids = {t.id for t in owners}
        membership = {v.id:v.catalog_title_id for v in s.scalars(select(Video))}
        outdated = _title('Anime/Changed/Season 1', _collection('Anime/Changed'),
                          part_type='season', season_number=9)
        changed_video = _video('Anime/Changed/Season 1/E01.mkv', title=outdated,
                               collection=outdated.collection, file_type='episode')
        obsolete = _title('Obsolete/title', _collection('Obsolete'))
        new_video = _video('Anime/New/Season 1/E01.mkv', file_type='episode')
        s.add_all([obsolete, new_video, changed_video]); s.commit()
        outdated_id = outdated.id
        obsolete_id, video_id = obsolete.id, new_video.id
        plan = rebuild.build_hierarchy_rebuild_plan(s)
        assert any(t.action == 'create' for t in plan.titles)
        assert any(t.action == 'update' and t.title_id == outdated_id for t in plan.titles)
        assert any(t.action == 'remove' and t.title_id == obsolete_id for t in plan.titles)
        assert not any(t.action == 'remove' and t.title_id in owner_ids for t in plan.titles)
        assert not s.new and not s.dirty and not s.deleted
        assert rebuild.apply_hierarchy_rebuild_plan(s, plan).applied
        s.commit()
    with Session(persisted_shared) as s:
        assert s.get(CatalogTitle, obsolete_id) is None
        assert s.get(CatalogTitle, outdated_id).season_number == 1
        assert owner_ids <= {t.id for t in s.scalars(select(CatalogTitle))}
        assert {id:s.get(Video, id).catalog_title_id for id in membership} == membership
        assert s.get(Video, video_id).catalog_title_id not in owner_ids | {obsolete_id}
        assert not rebuild.build_hierarchy_rebuild_plan(s).has_changes


def test_scanner_persisted_collision_preserves_existing_and_reviews_new(tmp_path,monkeypatch):
    from app.scanner import scan_library
    engine=v8_engine(tmp_path); migrations.migrate_schema_at_startup(engine)
    root=tmp_path/'media'
    folder=root/'Show'/'Season 02'; folder.mkdir(parents=True)
    (folder/'E01.mkv').write_bytes(b'video')
    (folder/'Show - S02P02E03.mkv').write_bytes(b'video')
    monkeypatch.setattr('app.scanner.service.probe_video',lambda *_args,**_kwargs:PROBE_RESULT)
    with Session(engine) as s:
        col=_collection('Show')
        a=_title('Show/Season 02',col,season_number=2,part_number=1)
        b=_title('Show/Season 02',col,season_number=2,part_number=2)
        v=_video('Show/Season 02/E01.mkv',title=b,collection=col,file_type='episode')
        s.add_all([a,b,v]); s.commit(); owner=b.id
        scan_library(s,root); s.commit()
    with Session(engine) as s:
        assert len(s.scalars(select(CatalogTitle)).all())==2
        rows={v.filename:v for v in s.scalars(select(Video))}
        assert rows['E01.mkv'].catalog_title_id==owner
        assert rows['Show - S02P02E03.mkv'].catalog_title_id is None
        assert s.scalar(select(CatalogCollection)).hierarchy_status=='review_required'


def test_startup_reconstruction_persisted_collisions_keeps_owners(persisted_shared):
    engine=persisted_shared
    with Session(engine) as s:
        titles={t.id:(t.relative_root_path,t.effective_season_number,t.effective_part_number) for t in s.scalars(select(CatalogTitle))}
        videos={v.id:v.catalog_title_id for v in s.scalars(select(Video))}
    migrations.migrate_schema(engine)
    migrations.migrate_schema(engine)
    with Session(engine) as s:
        assert {t.id:(t.relative_root_path,t.effective_season_number,t.effective_part_number) for t in s.scalars(select(CatalogTitle))}==titles
        assert {v.id:v.catalog_title_id for v in s.scalars(select(Video))}==videos


def test_virtual_allocator_reserves_all_hits_without_enforcing_normal_uniqueness(tmp_path):
    from app.hierarchy_review import create_title_from_videos
    engine=v8_engine(tmp_path); migrations.migrate_schema_at_startup(engine)
    with Session(engine) as s:
        col=_collection('Show'); other=_collection('Other')
        title=_title('Show/Season 02',col,part_type='season',season_number=2)
        v=_video('Show/Season 02/OVA 01.mkv',title=title,collection=col,file_type='ova')
        # Multiple old virtual hits in a different collection must reserve the handle.
        s.add_all([v,_title('Show/.catalog-part-2',other,id=0),_title('Show/.catalog-part-2',other,id=100)])
        s.commit()
        created=create_title_from_videos(s,col.id,[v.id],part_type='ova',season_number=2)
        s.commit()
        assert created.relative_root_path=='Show/.catalog-part-2-2'
        assert len(s.scalars(select(CatalogTitle).where(CatalogTitle.relative_root_path=='Show/.catalog-part-2')).all())==2


def test_reconstruction_preserves_extra_columns_indexes_triggers_and_rowid_contract(tmp_path):
    engine=v8_engine(tmp_path,autoincrement=True)
    seed_legacy(engine)
    with engine.begin() as c:
        c.exec_driver_sql("ALTER TABLE catalog_titles ADD COLUMN evidence VARCHAR NOT NULL DEFAULT 'legacy' CHECK(length(evidence)>0)")
        c.exec_driver_sql('CREATE UNIQUE INDEX ux_extra_title_name ON catalog_titles(local_title)')
        c.exec_driver_sql('CREATE INDEX ix_extra_title_evidence ON catalog_titles(evidence) WHERE evidence IS NOT NULL')
        c.exec_driver_sql("CREATE TRIGGER title_evidence_guard BEFORE UPDATE OF evidence ON catalog_titles WHEN NEW.evidence='forbidden' BEGIN SELECT RAISE(ABORT,'invalid evidence'); END")
        c.exec_driver_sql("UPDATE sqlite_sequence SET seq=1000 WHERE name='catalog_titles'")
        before=c.exec_driver_sql('SELECT * FROM catalog_titles ORDER BY id').all()
        objects=c.exec_driver_sql("SELECT name,sql FROM sqlite_schema WHERE tbl_name='catalog_titles' AND name IN ('ux_extra_title_name','ix_extra_title_evidence','title_evidence_guard') ORDER BY name").all()
    migrations.migrate_schema_at_startup(engine)
    with engine.connect() as c:
        assert c.exec_driver_sql('SELECT * FROM catalog_titles ORDER BY id').all()==before
        assert c.exec_driver_sql("SELECT name,sql FROM sqlite_schema WHERE tbl_name='catalog_titles' AND name IN ('ux_extra_title_name','ix_extra_title_evidence','title_evidence_guard') ORDER BY name").all()==objects
        assert 'AUTOINCREMENT' in c.scalar(text("SELECT sql FROM sqlite_schema WHERE name='catalog_titles'"))
        assert c.scalar(text("SELECT seq FROM sqlite_sequence WHERE name='catalog_titles'"))==1000
    with engine.begin() as c:
        with pytest.raises(IntegrityError): c.exec_driver_sql("UPDATE catalog_titles SET evidence='forbidden' WHERE id=1")
        with pytest.raises(IntegrityError): c.exec_driver_sql("UPDATE catalog_titles SET evidence='' WHERE id=1")
        with pytest.raises(IntegrityError): c.exec_driver_sql("UPDATE catalog_titles SET local_title='Title 1' WHERE id=1")


def test_ambiguous_legacy_migration_aborts_without_guessing(tmp_path):
    engine=v8_engine(tmp_path,locator_unique=False)
    seed_legacy(engine)
    with engine.begin() as c:
        c.exec_driver_sql("UPDATE catalog_titles SET relative_root_path='Old/0' WHERE id=2")
    before=schema(engine)
    with pytest.raises(RuntimeError,match='Ambiguous grouping title locator'):
        migrations.migrate_schema_at_startup(engine)
    assert schema(engine)==before
    with engine.connect() as c:
        assert c.scalar(text('PRAGMA user_version'))==8
        assert len(c.exec_driver_sql("SELECT id FROM catalog_titles WHERE relative_root_path='Old/0'").all())==2


def test_missing_historical_target_never_binds_to_later_collection(tmp_path):
    engine=v8_engine(tmp_path); seed_legacy(engine)
    with engine.begin() as c:
        c.exec_driver_sql("UPDATE collection_grouping_decisions SET target_collection_path='Absent target' WHERE id=1")
    migrations.migrate_schema_at_startup(engine)
    with Session(engine) as s:
        d=s.get(CollectionGroupingDecision,1)
        assert d.target_collection_id is None and d.target_collection_path=='Absent target'
        assert [r.catalog_title_id for r in d.selected_titles]==[1,2]
        s.add(_collection('Absent target')); s.commit()
    with Session(engine) as s:
        assert {t.id for t in collection_grouping_authority_targets(s)}==set(range(3,19))
        assert s.get(CollectionGroupingDecision,1).target_collection_id is None


def test_grouping_update_uses_same_decision_after_renames_and_latest_move_wins(tmp_path):
    from app.hierarchy_review import move_titles_to_collection, delete_empty_collection
    engine=make_engine(f"sqlite:///{tmp_path/'moves.db'}"); Base.metadata.create_all(engine)
    with Session(engine) as s:
        source,a,b=_collection('Source'),_collection('A'),_collection('B')
        title=_title('Source/title',source,part_type='ova')
        s.add_all([title,a,b]);s.commit()
        first=record_manual_collection_merge(s,a,[title.id]);s.commit();decision_id=first.id
        title.relative_root_path='A/shared';a.relative_root_path='renamed A';s.commit()
        again=record_manual_collection_merge(s,a,[title.id]);s.commit()
        assert again.id==decision_id
        assert len(s.scalars(select(CollectionGroupingDecision)).all())==1
        move_titles_to_collection(s,a.id,[title.id]);s.commit()
        move_titles_to_collection(s,b.id,[title.id]);record_manual_collection_merge(s,b,[title.id]);s.commit()
        assert title.collection is b
        assert {t.id:c.id for t,c in collection_grouping_authority_targets(s).items()}=={title.id:b.id}
        delete_empty_collection(s,a.id);s.commit()
        assert title.collection is b
        assert s.get(CollectionGroupingDecision,decision_id).target_collection_id==b.id


@pytest.mark.parametrize('workflow', ['manual', 'suggestion'])
def test_repeated_explicit_grouping_decision_regains_priority(tmp_path, workflow):
    from datetime import datetime
    from app.hierarchy_review import CollectionGroupingSuggestion, record_grouping_decision
    engine = make_engine(f"sqlite:///{tmp_path/'overlap.db'}")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as s:
        source, x, y = _collection('Source'), _collection('X'), _collection('Y')
        a, b = _title('Source/shared', source, 'A'), _title('Source/shared', source, 'B')
        s.add_all([a, b, x, y]); s.commit()

        def record(ids, target):
            if workflow == 'manual':
                return record_manual_collection_merge(s, target, ids)
            key = 'selection-' + '-'.join(map(str, ids))
            suggestion = CollectionGroupingSuggestion(
                key, f'target-{target.id}', 'Source', (source,), target.id, (),
            )
            record_grouping_decision(s, suggestion, 'merged',
                                     target_collection=target, selected_title_ids=ids)
            return s.scalar(select(CollectionGroupingDecision).where(
                CollectionGroupingDecision.suggestion_key == key))

        first = record([a.id, b.id], x); s.commit()
        first.updated_at = datetime(2000, 1, 1); s.commit()
        second = record([a.id], y); s.commit()
        second.updated_at = datetime(2001, 1, 1); s.commit()
        assert {t.id:c.id for t,c in collection_grouping_authority_targets(s).items()} == {
            a.id:y.id, b.id:x.id,
        }
        repeated = record([a.id, b.id], x); s.commit()
        assert repeated.id == first.id
        assert repeated.updated_at.replace(tzinfo=None) > second.updated_at.replace(tzinfo=None)
        assert {t.id:c.id for t,c in collection_grouping_authority_targets(s).items()} == {
            a.id:x.id, b.id:x.id,
        }


@pytest.mark.parametrize('size',[1,20,120])
def test_id_grouping_resolution_is_bounded_read_only_with_shared_locators(tmp_path,size):
    engine=make_engine('sqlite://');Base.metadata.create_all(engine)
    with Session(engine) as s:
        root=_collection('Root');target=_collection('Target')
        s.add_all([root,target]);s.flush()
        for n in range(size):
            title=_title('Root/Season 02',root,f'Owner {n}')
            s.add(title);s.flush()
            record_manual_collection_merge(s,target,[title.id])
        s.commit();target_id=target.id
    statements=[]
    def record(c,cursor,statement,*args):statements.append(statement)
    event.listen(engine,'before_cursor_execute',record)
    try:
        with Session(engine) as s:
            targets=collection_grouping_authority_targets(s)
            assert len(targets)==size
            assert {c.id for c in targets.values()}=={target_id}
            assert not s.new and not s.dirty and not s.deleted
    finally:event.remove(engine,'before_cursor_execute',record)
    assert all(sql.lstrip().upper().startswith('SELECT') for sql in statements)
    assert len(statements)<=8


def test_startup_unprotected_persisted_collision_does_not_overwrite_or_merge(tmp_path,monkeypatch):
    engine=v8_engine(tmp_path);migrations.migrate_schema_at_startup(engine)
    with Session(engine) as s:
        col=_collection('Show')
        for part in (1,2):
            title=_title('Show/Season 02',col,f'Owner {part}',part_type='season',season_number=2,part_number=part)
            s.add(_video(f'Show/Season 02/E{part:02}.mkv',title=title,collection=col,file_type='episode'))
        s.add(_video('Show/Season 02/Show - S02P02E03.mkv',collection=col,file_type='episode'))
        s.commit()
        ids={t.id:(t.local_title,t.part_number) for t in s.scalars(select(CatalogTitle))}
        owners={v.id:v.catalog_title_id for v in s.scalars(select(Video))}
    migrations.migrate_schema(engine)
    with Session(engine) as s:
        assert {t.id:(t.local_title,t.part_number) for t in s.scalars(select(CatalogTitle))}==ids
        assert {v.id:v.catalog_title_id for v in s.scalars(select(Video))}==owners
        assert s.scalar(select(CatalogCollection)).hierarchy_status=='review_required'


@pytest.mark.parametrize('protected',[True,False])
def test_manual_split_release_cannot_route_shared_locator_to_first_owner(persisted_shared,protected):
    from app.hierarchy_review import apply_manual_split
    from app.manual_split import definition_from_title
    from dataclasses import replace
    with Session(persisted_shared) as s:
        titles=s.scalars(select(CatalogTitle).order_by(CatalogTitle.id)).all()
        a=titles[0]; video=a.videos[0]; video_id=video.id; owner_id=a.id
        if not protected:
            a.hierarchy_manual_override=False
            a.part_type='part'
            a.part_type_manual=None; a.season_number_manual=None; a.part_number_manual=None
        definitions=[replace(definition_from_title(t),video_ids=tuple(link.video_id for link in t.manual_split_rule_videos) if t is not a else ()) for t in titles]
        apply_manual_split(s,a.catalog_collection_id,definitions)
        s.commit()
    with Session(persisted_shared) as s:
        assert s.get(Video,video_id).catalog_title_id == (owner_id if protected else None)
        assert s.get(Video,video_id).manual_split_rule_videos == []
        assert len(s.scalars(select(CatalogTitle)).all())==len(titles)


def test_metadata_split_with_shared_locator_keeps_original_ids_and_choices(tmp_path):
    from app.metadata.split import apply_metadata_split
    from app.physical_naming_service import confirm_physical_naming_choice
    from app.physical_layout_service import confirm_physical_layout_choice
    from test_metadata_split import supplementary_title,attach_confirmed_metadata
    from test_physical_naming import NOW
    engine=v8_engine(tmp_path);migrations.migrate_schema_at_startup(engine)
    with Session(engine) as s:
        root,source,_=supplementary_title([f'Special {n:02}.mkv' for n in range(1,7)])
        attach_confirmed_metadata(source,3)
        another=_title(source.relative_root_path,root,'Separate owner',part_type='bonus')
        s.add(root);s.commit();source_id,other_id=source.id,another.id
        naming=confirm_physical_naming_choice(s,source,'Source','custom',now=NOW)
        layout=confirm_physical_layout_choice(s,source,'own_folder',now=NOW)
        s.commit();choice_ids=naming.id,layout.id
        result=apply_metadata_split(s,source.id,confirmed=True);s.commit();new_id=result.new_title.id
    with Session(engine) as s:
        source=s.get(CatalogTitle,source_id)
        assert s.get(CatalogTitle,other_id) is not None
        assert new_id not in {source_id,other_id}
        assert len(source.videos)==3 and len(s.get(CatalogTitle,new_id).videos)==3
        assert (source.physical_naming_choice.id,source.physical_layout_choice.id)==choice_ids
        assert s.get(CatalogTitle,new_id).physical_naming_choice is None
        assert s.get(CatalogTitle,new_id).physical_layout_choice is None


@pytest.mark.parametrize('deleted',['title','target'])
def test_loaded_grouping_authority_cannot_rebind_reused_id_in_same_session(tmp_path,deleted):
    engine=make_engine('sqlite://');Base.metadata.create_all(engine)
    with Session(engine,expire_on_commit=False) as s:
        source,target=_collection('Source'),_collection('Target')
        owner=_title('Source/title',source)
        s.add_all([owner,target]);s.commit()
        d=record_manual_collection_merge(s,target,[owner.id]);s.commit()
        assert collection_grouping_authority_targets(s)
        if deleted=='title':
            owner_id=owner.id
            s.delete(owner);s.commit()
            s.add(_title('Source/title',source,id=owner_id));s.commit()
        else:
            target_id=target.id
            s.delete(target);s.commit()
            s.add(_collection('Target',id=target_id));s.commit()
        assert collection_grouping_authority_targets(s)=={}


def test_id_grouping_key_cannot_overwrite_legacy_numeric_path_decision(tmp_path):
    from app.hierarchy_review import _digest
    engine=v8_engine(tmp_path)
    with Session(engine) as s:
        source,target=_collection('Source'),_collection('Target')
        new=_title('Source/current',source,id=1)
        old=_title('1',source,id=2)
        s.add_all([new,old,target]);s.commit();target_id=target.id
    with engine.begin() as c:
        c.exec_driver_sql("INSERT INTO collection_grouping_decisions (suggestion_key,state_fingerprint,decision,target_collection_path,selected_title_paths_json,created_at,updated_at) VALUES (?,'legacy','merged','Target','[\"1\"]','2000-01-01','2000-01-01')",(_digest(['manual-collection-merge','1']),))
    migrations.migrate_schema_at_startup(engine)
    with Session(engine) as s:
        record_manual_collection_merge(s,s.get(CatalogCollection,target_id),[1]);s.commit()
    with Session(engine) as s:
        assert len(s.scalars(select(CollectionGroupingDecision)).all())==2
        assert {t.id:c.id for t,c in collection_grouping_authority_targets(s).items()}=={1:target_id,2:target_id}


def test_application_startup_failure_rolls_back_whole_v8_schema(tmp_path):
    import asyncio
    from app.config import Settings
    from app.main import create_app
    engine=v8_engine(tmp_path);seed_legacy(engine)
    before=schema(engine)
    app=create_app(Settings(anime_path=tmp_path/'media',database_url=f"sqlite:///{tmp_path/'old.db'}",metadata_download_artwork=False))
    app_engine=app.state.sessions.kw['bind']
    def inject(c,cursor,statement,*args):
        if statement.upper().startswith('INSERT INTO CATALOG_TITLES_LOCATOR_MIGRATION'):
            raise RuntimeError('startup copy failure')
    async def startup():
        async with app.router.lifespan_context(app):
            pass
    event.listen(app_engine,'before_cursor_execute',inject)
    try:
        with pytest.raises(RuntimeError,match='startup copy failure'):
            asyncio.run(startup())
    finally:event.remove(app_engine,'before_cursor_execute',inject)
    assert schema(app_engine)==before
    with app_engine.connect() as c:
        assert c.scalar(text('PRAGMA user_version'))==8
        assert c.scalar(text('PRAGMA foreign_keys'))==1
