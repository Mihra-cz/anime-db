"""Historical parser evidence and current physical names may deliberately differ."""
import importlib
from pathlib import PurePosixPath

from starlette.requests import Request

from app.catalog import detect_episode_number, sort_title_videos
from app.models import UnresolvedExternalSubtitle, Video
from app.numbering import preview_sequential_numbering
from app.subtitle_review import build_subtitle_candidate_index, subtitle_candidates
from test_physical_naming import session, collection, title


def video(*, id=1, source='Release NCED02.mkv', physical='Show - S01 - NCED 02 [BD].mkv'):
    return Video(id=id, filename=source, relative_path=f'Show/Season 01/{physical}',
                 root_folder='Show', file_type='nced', size=1, mtime_ns=1)


def test_physical_filename_is_a_pure_locator_read():
    paths = importlib.import_module('app.video_paths')
    v = video(source='old/source.mkv', physical='日本語 [BD].MKV')
    before = dict(v.__dict__)
    assert paths.current_physical_filename(v) == '日本語 [BD].MKV'
    assert v.__dict__ == before


def test_naming_extension_uses_current_physical_locator():
    from app.naming_review_service import _known_files
    from app.models import CatalogTitle
    t = CatalogTitle(part_type='season', season_number=1, season_label='S1')
    v = video(source='Release - 01.mkv', physical='Show - S01E01.webm')
    v.file_type = 'episode'
    v.season_episode_number = v.local_episode_number = 1
    t.videos.append(v)
    known, deferred = _known_files(t)
    assert deferred == 0 and known[0].extension == '.webm'
    assert v.filename == 'Release - 01.mkv'


def test_subtitle_name_similarity_uses_physical_name_but_numbering_keeps_source():
    v = video()
    index = build_subtitle_candidate_index([v])
    assert index.normalized_stems[v] == 'show s01 nced 02 bd'
    assert index.detections[v] == detect_episode_number(v.filename)
    sub = UnresolvedExternalSubtitle(id=1, relative_path='Show/Season 01/Show - S01 - NCED 02 [BD].ass',
        filename='Show - S01 - NCED 02 [BD].ass', extension='.ass', status='unresolved')
    _, candidates, _ = subtitle_candidates(sub, [v], candidate_index=index)
    assert 'podobnost názvu 100 %' in candidates[0].reasons
    assert v.filename == 'Release NCED02.mkv'


def test_sort_by_filename_uses_current_physical_names():
    first = video(id=1, source='Z source.mkv', physical='A current.mkv')
    second = video(id=2, source='A source.mkv', physical='Z current.mkv')
    ordered, sort, _ = sort_title_videos([second, first], 'filename', 'asc')
    assert ordered == [first, second] and sort == 'filename'


def test_numbering_preview_displays_physical_name_without_changing_source_order():
    v = video(source='Release - 01.mkv', physical='Show - S01E01 [BD].mkv')
    v.file_type = 'episode'
    v.season_episode_number = v.local_episode_number = 1
    row, = preview_sequential_numbering([v], 1)
    assert row.filename == PurePosixPath(v.relative_path).name
    assert v.filename == 'Release - 01.mkv'


def test_media_edit_label_uses_current_physical_name():
    from app.media_edit_save import _video_label
    v = video()
    assert _video_label(v) == 'Video · Show - S01 - NCED 02 [BD].mkv'


def test_part_local_proposal_lists_current_physical_representations(session):
    from test_part_local_numbering import build
    from app.part_local_numbering import evaluate_part_local_numbering
    _, titles = build(session, [('P1',1,1,range(1,3),2), ('P2',1,2,range(3,5),2)])
    t = titles['P2']
    for n,v in enumerate(t.videos,1):
        v.relative_path = f'Show/Season 01/Show - S01P02E{n:02}.mkv'
    proposal = evaluate_part_local_numbering(t).proposal
    assert proposal is not None
    assert {f for row in proposal.rows for f in row.filenames} == {
        PurePosixPath(v.relative_path).name for v in t.videos}
    assert {v.filename for v in t.videos} == {'Show - 03.mkv','Show - 04.mkv'}


def test_bulk_renumber_proposal_labels_current_files_without_reparsing_them():
    from test_recap_fractional_numbering import _graph
    from app.numbering import deterministic_bulk_renumber_proposal
    _, t, videos, _ = _graph((*range(1,15),*range(16,26)))
    for v in videos:
        v.relative_path = f'Show/Season 01/current-{v.id}.mkv'
    proposal = deterministic_bulk_renumber_proposal(t)
    assert proposal is not None and proposal.offset == -1
    by_id = {v.id:v for v in videos}
    assert all(change.filename == PurePosixPath(by_id[change.video_id].relative_path).name
        for row in proposal.rows for change in row.physical_changes)


def test_variant_preview_separates_physical_label_from_parser_hint(session):
    from app.video_variants import preview_video_variant_assignments
    root = collection(session)
    main = title(session, root)
    v = Video(catalog_collection=root, catalog_title=main, filename='Release - 01 Ver.TV.mkv',
        relative_path='Show/Season 01/Show - S01E01 [TV].mkv', root_folder='Show',
        file_type='episode', size=1, mtime_ns=1, local_episode_number=1, season_episode_number=1)
    session.add(v); session.commit()
    preview = preview_video_variant_assignments(session, root.id, main.id,
        assignments=((v.id, 'null'),), drafts=())
    row, = preview.rows
    assert row.filename == 'Show - S01E01 [TV].mkv'
    assert row.parser_hint == 'Ver.TV'
    assert v.filename == 'Release - 01 Ver.TV.mkv'


def test_folder_file_label_uses_current_name():
    from app.main import app, templates
    from app.catalog import translation_status
    request = Request({'type':'http', 'method':'GET', 'path':'/', 'root_path':'',
        'scheme':'http', 'query_string':b'', 'headers':[], 'server':('testserver',80), 'app':app})
    v = video()
    rendered = templates.env.get_template('folder.html').render(
        request=request, folder='Show', videos=[v], translation_status=translation_status)
    assert '>Show - S01 - NCED 02 [BD].mkv<small>' in rendered
    assert v.filename == 'Release NCED02.mkv'


def test_readonly_title_file_label_uses_locator_without_rewriting_evidence(tmp_path):
    from test_title_detail_readonly import _title_detail_app, _render_title_detail
    from sqlalchemy import event
    app, (_, title_id) = _title_detail_app(tmp_path, confirmed=True)
    with app.state.sessions() as db:
        v = db.query(Video).one()
        v.filename = 'Original release - 01.mkv'
        v.relative_path = 'Anime/Catalog Show/Season 1/Catalog Show - S01E01.mkv'
        db.commit()
        before = tuple(getattr(v, c.name) for c in Video.__table__.columns)
        engine = db.get_bind()
    writes = []
    def check(connection, cursor, statement, parameters, context, many):
        if statement.lstrip().upper().startswith(('UPDATE','INSERT','DELETE')):
            writes.append(statement)
    event.listen(engine, 'before_cursor_execute', check)
    try:
        rendered = _render_title_detail(app, title_id)
        assert '<strong>Catalog Show - S01E01.mkv</strong>' in rendered
        assert writes == []
        with app.state.sessions() as db:
            v = db.query(Video).one()
            assert tuple(getattr(v, c.name) for c in Video.__table__.columns) == before
    finally:
        event.remove(engine, 'before_cursor_execute', check)


def test_hierarchy_variant_controls_parse_source_evidence_and_display_current_name(tmp_path, monkeypatch):
    from app.main import templates
    from test_title_detail_readonly import _title_detail_app
    from test_video_variant_authority_ui import _get_request
    app, (collection_id, _) = _title_detail_app(tmp_path, confirmed=True)
    source = 'Nande Koko ni Sensei ga! - NCED2.mp4'
    physical = 'Nande Koko ni Sensei ga! - S01 - NCED 02 [BD].mp4'
    with app.state.sessions() as db:
        v = db.query(Video).one()
        v.filename = source
        v.relative_path = f'Anime/Catalog Show/Season 1/{physical}'
        v.file_type = 'nced'
        v.local_episode_number = v.season_episode_number = None
        db.commit()
    parsed = []
    def parse(filename):
        parsed.append(filename)
        return detect_episode_number(filename)
    monkeypatch.setitem(templates.env.globals, 'detect_episode_number', parse)
    endpoint = next(r.endpoint for r in app.routes if getattr(r,'path',None)=='/hierarchy-review/{collection_id}')
    rendered = endpoint(_get_request(app,f'/hierarchy-review/{collection_id}'), collection_id).body.decode()
    assert f'<strong>{physical}</strong>' in rendered
    assert source in parsed
    assert physical not in parsed
