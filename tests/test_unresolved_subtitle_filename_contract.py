"""Unresolved source/parser evidence survives physical presentation and reload."""
from sqlalchemy import event

from app.catalog import detect_episode_number
from app.models import UnresolvedExternalSubtitle
from app.subtitle_review import build_subtitle_candidate_index, subtitle_candidates
from test_video_filename_contract import video


def test_name_similarity_reads_current_subtitle_basename_and_parser_reads_source():
    v=video()
    sub=UnresolvedExternalSubtitle(id=1, filename='Release NCED02.ass',
        relative_path='Show/Season 01/Show - S01 - NCED 02 [BD].ass', extension='.ass', status='unresolved')
    before=dict(sub.__dict__)
    _, candidates, _=subtitle_candidates(sub, [v], candidate_index=build_subtitle_candidate_index([v]))
    assert 'podobnost názvu 100 %' in candidates[0].reasons
    assert 'shodný supplementary typ' in candidates[0].reasons[0]
    assert detect_episode_number(sub.filename).supplementary_number == 2
    assert sub.__dict__ == before


def test_media_check_subtitle_label_reads_locator_without_rewriting_source(tmp_path):
    from test_media_check import _media_app, _request
    web, *_=_media_app(tmp_path)
    with web.state.sessions() as db:
        sub=UnresolvedExternalSubtitle(relative_path='Anime/Partial Translation/Season 1/Current subtitle.ass',
            filename='Historical parser evidence.ass', extension='.ass', status='confirmed_no_match')
        db.add(sub); db.commit()
        id=sub.id
        db.refresh(sub)
        before=tuple(getattr(sub, c.name) for c in sub.__table__.columns)
    sql=[]
    def read_only(conn, cursor, statement, *args):
        assert statement.lstrip().upper().startswith('SELECT'), statement
        sql.append(statement)
    engine=web.state.sessions.kw['bind']
    event.listen(engine, 'before_cursor_execute', read_only)
    try:
        endpoint=next(r.endpoint for r in web.routes if getattr(r, 'path', None) == '/media-check')
        html=endpoint(_request(web, '/media-check'), subtitle='all', audio='all', q='', page=1, message=None).body.decode()
        assert '<strong>Current subtitle.ass</strong>' in html
        assert '<strong>Historical parser evidence.ass</strong>' not in html
        with web.state.sessions() as db:
            sub=db.get(UnresolvedExternalSubtitle, id)
            assert tuple(getattr(sub, c.name) for c in sub.__table__.columns) == before
    finally:
        event.remove(engine, 'before_cursor_execute', read_only)


def test_unresolved_number_hint_comes_from_source_filename_not_physical_name():
    from app.models import Video
    def episode(n):
        return Video(id=n, filename=f'Release - {n:02}.mkv', relative_path=f'Show/Season 01/Show - S01E{n:02}.mkv',
                     root_folder='Show', file_type='episode', size=1, mtime_ns=1)
    videos=[episode(7), episode(8)]
    # The current physical name carries no parser evidence; only the source does.
    sub=UnresolvedExternalSubtitle(id=1, filename='Release - 07.ass', relative_path='Show/Season 01/subtitle.ass',
        extension='.ass', status='unresolved')
    scope, candidates, _=subtitle_candidates(sub, videos, candidate_index=build_subtitle_candidate_index(videos))
    assert 'shodný číselný hint' in scope
    assert [c.video.id for c in candidates] == [7]
    assert sub.filename == 'Release - 07.ass'
