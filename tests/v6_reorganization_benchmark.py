"""Local synthetic benchmark; run with --output /tmp/report.json (no NAS).

Compares full planner results with frozen pre-index algorithm at 0/190/950
auxiliary files. Web measurement uses real SQLite/media, preflight, inventory,
HTML and a real disposable preview process. No application lifespan runs.
"""
import argparse
from collections import Counter
from dataclasses import asdict
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
from app import target_planner_filesystem as planner
from test_reorganization_planner_index import synthetic_evidence
from target_planner_namespace_reference import _namespace_collisions as reference


def planner_case(count, mode, managed):
    context, snapshot = synthetic_evidence(count, managed=managed)
    start = time.perf_counter()
    with patch.object(planner, '_namespace_collisions', reference if mode == 'before' else planner._namespace_collisions):
        result = planner.plan_library(context, snapshot)
    return dict(auxiliary=count, mode=mode, managed=managed, seconds=time.perf_counter()-start,
                rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                plan_hash=result.plan_hash, records=len(result.records))


def web_case():
    os.environ.update(ANIME_PATH='/tmp', DATABASE_URL='sqlite://', METADATA_ARTWORK_DIRECTORY='/tmp/anime-reorg-benchmark-artwork')
    from app.config import Settings
    from app.database import Base
    from app.main import create_app
    from app.reorganization_web import build_preview
    from test_naming_review_web import EndpointClient
    from test_target_planner_service import seed
    with tempfile.TemporaryDirectory(prefix='anime-web-bench-') as directory:
        root = Path(directory)
        media = root/'media'
        media.mkdir()
        db = root/'bench.db'
        app = create_app(Settings(anime_path=media, database_url=f'sqlite:///{db}',
            metadata_artwork_directory=root/'artwork', metadata_download_artwork=False))
        Base.metadata.create_all(app.state.sessions.kw['bind'])
        with app.state.sessions() as session:
            _, _, videos = seed(session, count=2000)
            for video in videos:
                path = media/video.relative_path
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(b'video')
                path.with_suffix('.ass').write_text('subtitle')
        extras = media/'Source/Extras'
        extras.mkdir()
        for i in range(950):
            (extras/f'asset{i}.flac').write_bytes(b'asset')
        operations = Counter()
        real_scandir, real_pathconf, real_lstat = os.scandir, os.pathconf, Path.lstat
        class Entry:
            def __init__(self, entry):
                self.entry, self.name, self.path = entry, entry.name, entry.path
            def stat(self, **kwargs):
                operations['entry.stat'] += 1
                return self.entry.stat(**kwargs)
        class Scan:
            def __init__(self, path):
                operations['scandir'] += 1
                self.stream = real_scandir(path)
            def __enter__(self):
                return (Entry(e) for e in self.stream)
            def __exit__(self, *args):
                self.stream.close()
        def pathconf(*args):
            operations['pathconf'] += 1
            return real_pathconf(*args)
        def lstat(*args, **kwargs):
            operations['path.lstat'] += 1
            return real_lstat(*args, **kwargs)
        start = time.perf_counter()
        with patch.object(os, 'scandir', Scan), patch.object(os, 'pathconf', pathconf), patch.object(Path, 'lstat', lstat):
            result = build_preview(db, media)
        direct_seconds = time.perf_counter()-start
        client = EndpointClient(app)
        start = time.perf_counter()
        response = client.post('/reorganization/preview')
        assert response.status_code == 200
        return dict(records=len(result['plan']['records']), inventory_and_plan_seconds=direct_seconds,
            real_post_seconds=time.perf_counter()-start, response_bytes=len(response.content),
            parent_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            children_peak_rss_kib=resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
            python_filesystem_operations=dict(operations),
            operations_scope='Direct build includes inventory/DB locators; health-check child and SQLite native syscalls excluded.',
            file_content_hashes=0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--managed', type=int, default=2000)
    parser.add_argument('--case', type=int)
    parser.add_argument('--mode', choices=['before', 'after'])
    parser.add_argument('--web', action='store_true')
    args = parser.parse_args()
    if args.case is not None:
        print(json.dumps(planner_case(args.case, args.mode, args.managed)))
        return
    if args.web:
        print(json.dumps(web_case()))
        return
    results = []
    for count in (0, 190, 950):
        for mode in ('before', 'after'):
            run = subprocess.run([sys.executable, '-B', __file__, '--case', str(count), '--mode', mode,
                '--managed', str(args.managed)], capture_output=True, check=True, text=True, timeout=240)
            row = json.loads(run.stdout)
            results.append(row)
            print(json.dumps(row), flush=True)
        assert results[-1]['plan_hash'] == results[-2]['plan_hash']
    web = subprocess.run([sys.executable, '-B', __file__, '--web'], capture_output=True, check=True, text=True, timeout=120)
    report = dict(planner=results, web=json.loads(web.stdout))
    if args.output:
        args.output.write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(report['web']), flush=True)


if __name__ == '__main__':
    main()
