"""Batch DB evidence never flushes, writes or loads per physical asset."""
import importlib
import importlib.util
from dataclasses import asdict

import pytest
from sqlalchemy import event, select

from app.models import Video, ExternalSubtitle, ExternalSubtitleCompatibility
from test_physical_naming import session, collection, title, NOW, attach_metadata


def api():
    assert importlib.util.find_spec('app.target_planner_service') is not None, 'Planner batch loader missing'
    return importlib.import_module('app.target_planner_service')


def seed(session, count=3):
    root=collection(session,'Source')
    main=title(session,root,metadata=False)
    attach_metadata(main,str(main.id),'Canonical')
    videos=[]
    for n in range(1,count+1):
        v=Video(catalog_title=main,catalog_collection=root,root_folder='Source',relative_path=f'Source/video{n}.mkv',filename=f'video{n}.mkv',size=1,mtime_ns=1,file_type='episode',season_episode_number=n,local_episode_number=n)
        session.add(v); videos.append(v)
    session.flush()
    for v in videos:
        sub=ExternalSubtitle(relative_path=v.relative_path.replace('.mkv','.ass'),codec='ass')
        session.add(sub);session.flush()
        session.add(ExternalSubtitleCompatibility(external_subtitle=sub,video=v,status='confirmed_compatible',match_method='manual',verified_at=NOW))
    session.commit()
    return root,main,videos


def test_loader_is_select_only_scalar_and_session_stays_clean(session):
    svc=api()
    seed(session)
    session.expunge_all()
    sql=[]
    def record(conn,cursor,statement,params,context,executemany):
        sql.append(statement)
        assert statement.lstrip().upper().startswith('SELECT'), statement
    event.listen(session.get_bind(),'before_cursor_execute',record)
    try:
        ctx=svc.load_planner_context(session)
        assert len(ctx.videos)==3 and len(ctx.subtitles)==3
        from app.target_planner import project_targets
        records=project_targets(ctx)
        assert next(r for r in records if r.object_kind=='video').target_relative_path.startswith('Canonical/Season 01/Canonical - S01E')
        assert len(sql)==11
        assert (len(session.new),len(session.dirty),len(session.deleted))==(0,0,0)
        session.expunge_all()
        assert records==project_targets(ctx)
    finally:
        event.remove(session.get_bind(),'before_cursor_execute',record)


@pytest.mark.parametrize('count',[2,620])
def test_query_count_does_not_grow_with_video_or_subtitle_rows(session,count):
    svc=api();seed(session,count)
    session.expunge_all()
    sql=[]
    def record(*args):sql.append(args[2])
    event.listen(session.get_bind(),'before_cursor_execute',record)
    try:
        ctx=svc.load_planner_context(session)
        assert len(ctx.videos)==count and len(ctx.subtitles)==count
        assert len(sql)==11
    finally:event.remove(session.get_bind(),'before_cursor_execute',record)


def test_loader_refuses_pending_business_writes_without_autoflush(session):
    svc=api();root,_,_=seed(session)
    root.local_title='pending edit'
    sql=[]
    def record(*args):sql.append(args[2])
    event.listen(session.get_bind(),'before_cursor_execute',record)
    try:
        with pytest.raises(ValueError,match='clean'):
            svc.load_planner_context(session)
        assert sql==[]
    finally:event.remove(session.get_bind(),'before_cursor_execute',record)
    session.rollback()
    assert root.local_title=='Source'


def test_readonly_sqlite_entrypoint_preserves_fingerprint_and_has_no_startup(tmp_path,session):
    svc=api()
    seed(session)
    path=tmp_path/'fixture.db'
    import sqlite3,hashlib
    target=sqlite3.connect(path)
    session.connection().connection.driver_connection.backup(target)
    target.close()
    before=(path.stat().st_size,path.stat().st_mtime_ns,hashlib.sha256(path.read_bytes()).hexdigest())
    with svc.readonly_planner_session(path) as ro:
        ctx=svc.load_planner_context(ro)
        assert len(ctx.videos)==3
        assert (len(ro.new),len(ro.dirty),len(ro.deleted))==(0,0,0)
        with pytest.raises(Exception,match='readonly'):
            ro.connection().exec_driver_sql("UPDATE videos SET size=2")
    assert (path.stat().st_size,path.stat().st_mtime_ns,hashlib.sha256(path.read_bytes()).hexdigest())==before


def test_cli_readonly_simulation_repeats_and_checks_determinism(tmp_path,session):
    assert importlib.util.find_spec('app.tools.target_plan') is not None, 'Read-only planner CLI missing'
    cli=importlib.import_module('app.tools.target_plan')
    _,_,videos=seed(session)
    path=tmp_path/'fixture.db'
    import sqlite3
    target=sqlite3.connect(path)
    session.connection().connection.driver_connection.backup(target)
    target.close()
    root=tmp_path/'media'
    for v in videos:
        source=root/v.relative_path;source.parent.mkdir(parents=True,exist_ok=True);source.write_bytes(b'video')
        source.with_suffix('.ass').write_text('subtitle')
    result=cli.simulate_readonly(path,root,repeat=2)
    assert result['deterministic'] is True
    assert result['sql_select_counts']==[11,11]
    assert result['session_states']==[[0,0,0],[0,0,0]]
    assert len(set(result['plan_hashes']))==1
    assert result['production_db_unchanged'] is True
    assert dict(result['plan']['status_counts'])=={'BLOCKED':0,'READY':6,'REVIEW':0,'WARNING':0}


def test_readonly_loader_uses_a_consistent_database_read_transaction(tmp_path,session):
    svc=api();seed(session)
    import sqlite3
    path=tmp_path/'snapshot.db'
    target=sqlite3.connect(path)
    session.connection().connection.driver_connection.backup(target)
    target.close()
    with svc.readonly_planner_session(path) as ro:
        ctx=svc.load_planner_context(ro)
        assert len(ctx.videos)==3
        assert ro.connection().connection.driver_connection.in_transaction is True


def test_cli_exit_status_distinguishes_clean_from_blocked_plan(tmp_path,session,capsys):
    cli=importlib.import_module('app.tools.target_plan')
    _,_,videos=seed(session)
    path=tmp_path/'fixture.db'
    import sqlite3
    target=sqlite3.connect(path)
    session.connection().connection.driver_connection.backup(target)
    target.close()
    root=tmp_path/'media'
    for v in videos:
        source=root/v.relative_path;source.parent.mkdir(parents=True,exist_ok=True);source.write_bytes(b'video')
        source.with_suffix('.ass').write_text('subtitle')
    args=['--db',str(path),'--library-root',str(root),'--repeat','1']
    assert cli.main(args)==0
    (root/videos[0].relative_path).unlink()
    assert cli.main(args)==2
    capsys.readouterr()
    clean=dict(deterministic=True,production_db_unchanged=True,plan=dict(status_counts=[('BLOCKED',0),('REVIEW',0)]))
    assert cli.exit_status(clean)==0
    assert cli.exit_status(dict(clean,deterministic=False))==1
    assert cli.exit_status(dict(clean,production_db_unchanged=False))==1
    assert cli.exit_status(dict(clean,plan=dict(status_counts=[('BLOCKED',0),('REVIEW',1)])))==2


def test_cli_post_state_projection_is_readonly_and_settled(tmp_path, session):
    cli=importlib.import_module('app.tools.target_plan')
    _,_,videos=seed(session)
    path=tmp_path/'fixture.db'
    import sqlite3
    target=sqlite3.connect(path)
    session.connection().connection.driver_connection.backup(target)
    target.close()
    root=tmp_path/'media'
    for v in videos:
        source=root/v.relative_path
        source.parent.mkdir(parents=True,exist_ok=True)
        source.write_bytes(b'video')
        source.with_suffix('.ass').write_text('subtitle')
    assert 'post_state' in __import__('inspect').signature(cli.simulate_readonly).parameters, 'Read-only post-state CLI missing'
    result=cli.simulate_readonly(path,root,repeat=2,post_state=True)
    assert result['post_state_deterministic'] is True
    assert len(set(result['post_state_hashes']))==1
    assert result['post_state']['converged'] is True
    assert result['post_state']['blockers']==[]
    assert all(r['action']=='KEEP' for r in result['post_state']['plan']['records'])
    assert result['sql_select_counts']==[11,11]
    assert result['session_states']==[[0,0,0],[0,0,0]]
    assert result['production_db_unchanged'] is True
    assert cli.exit_status(result)==0
    broken=dict(result,post_state=dict(result['post_state'],converged=False))
    assert cli.exit_status(broken)==2


@pytest.mark.parametrize('source_members,content', [
    (tuple((f'Nande Koko ni Sensei ga! - NCED{n}.mp4', 'BD', n) for n in range(1,6)), 'nced'),
    (tuple((f'Nande Koko ni Sensei ga! - NCOP Ver.TV{n}.mp4', 'TV', n) for n in range(1,5)), 'ncop'),
    (tuple((f'Nande Koko ni Sensei ga! - NCOP{n}.mp4', 'BD', n) for n in range(1,5)), 'ncop'),
    ((('[Judas] Tensei Shitara Slime Datta Ken - NCED 02a.mkv', 'A', 2),
      ('[Judas] Tensei Shitara Slime Datta Ken - NCED 02b.mkv', 'B', 2)), 'nced'),
])
def test_locator_only_fixture_reload_keeps_original_parser_evidence(session, source_members, content):
    """A physical locator patch must not discard existing derived ordinals."""
    from pathlib import PurePosixPath
    from app.models import VideoVariantGroup
    from app.target_planner import project_container_locators
    from app.target_planner_filesystem import plan_library
    from app.target_planner_post_state import simulate_post_state
    from app.target_planner_types import FilesystemEntry,FilesystemSnapshot
    from app.database import Base
    def domain_snapshot(session):
        return {t.name: tuple(tuple(r) for r in session.execute(t.select().order_by(*t.primary_key.columns)))
                for t in Base.metadata.sorted_tables}
    root=collection(session,'Source')
    main=title(session,root)
    bonus=title(session,root,'NC',kind='bonus',metadata=False)
    groups={label: VideoVariantGroup(catalog_title=bonus,manual_label=label,verified_at=NOW)
            for label in sorted({label for _,label,_ in source_members})}
    members=[Video(catalog_title=main,catalog_collection=root,root_folder='Source',relative_path='Source/ep01.mkv',filename='ep01.mkv',file_type='episode',size=1,mtime_ns=1,local_episode_number=1,season_episode_number=1)]
    for filename,label,_ in source_members:
        members.append(Video(catalog_title=bonus,catalog_collection=root,video_variant_group=groups[label],root_folder='Source',relative_path=f'Source/NC/{filename}',filename=filename,file_type=content,size=1,mtime_ns=1))
    session.add_all(members);session.commit();session.expunge_all()
    before=domain_snapshot(session)
    ctx=api().load_planner_context(session)
    assert [v.ordinal for v in ctx.videos if v.title_id==bonus.id]==[n for _,_,n in source_members]
    assert [v.source_evidence_filename for v in ctx.videos]==['ep01.mkv']+[f for f,_,_ in source_members]
    snap=FilesystemSnapshot('/library',tuple(FilesystemEntry(v.source,'regular',1,1) for v in ctx.videos),255,4096)
    current=plan_library(ctx,snap)
    projected=simulate_post_state(ctx,snap,current)
    assert projected.converged
    assert all(old.source != new.source for old,new in zip(ctx.videos,projected.context.videos))
    assert [v.source_evidence_filename for v in projected.context.videos]==[v.source_evidence_filename for v in ctx.videos]
    from dataclasses import replace
    assert all(replace(new,source=old.source)==old for old,new in zip(ctx.videos,projected.context.videos))
    for locator in project_container_locators(ctx):
        table='catalog_collections' if locator.object_kind=='collection' else 'catalog_titles'
        session.connection().exec_driver_sql(f'UPDATE {table} SET relative_root_path=? WHERE id=?',(locator.target_relative_path,locator.object_id))
    for row in current.records:
        target=PurePosixPath(row.target_relative_path)
        session.connection().exec_driver_sql('UPDATE videos SET relative_path=?,root_folder=? WHERE id=?',(str(target),target.parts[0],row.object_id))
    session.commit();session.expunge_all()
    loaded=api().load_planner_context(session)
    after=domain_snapshot(session)
    assert loaded.videos==projected.context.videos
    assert plan_library(loaded,projected.snapshot)==projected.plan
    allowed={'catalog_collections':{'relative_root_path'},'catalog_titles':{'relative_root_path'},'videos':{'relative_path','root_folder'}}
    for table in Base.metadata.sorted_tables:
        fields=[c.name for c in table.columns]
        retained=[i for i,f in enumerate(fields) if f not in allowed.get(table.name,set())]
        assert [tuple(row[i] for i in retained) for row in before[table.name]]==[tuple(row[i] for i in retained) for row in after[table.name]]
    assert (len(session.new),len(session.dirty),len(session.deleted))==(0,0,0)
