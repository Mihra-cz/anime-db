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
