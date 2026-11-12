"""LAB ONLY: synthetic SQLite v9 and library for the isolated V6 browser lab.

Reuses the existing test fixture builders (tests/test_physical_naming.py and the
v6_database_case/production/case patterns). Never reads production data. The
result is checked with the real Target Planner: no REVIEW/BLOCKED, no collisions,
at least one MOVE, QUARANTINE and WARNING.

    python deploy/lab/make_lab_data.py --out DIR    # creates DIR/library, DIR/anime.db
"""
import argparse
from collections import Counter
from pathlib import Path
import os
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]

from sqlalchemy.orm import Session  # noqa: E402

from app.database import Base, make_engine  # noqa: E402
from app.models import (  # noqa: E402
    ExternalSubtitle, ExternalSubtitleCompatibility, UnresolvedExternalSubtitle, Video,
)
from app.target_planner import project_targets  # noqa: E402
from app.target_planner_filesystem import inventory_filesystem, plan_library  # noqa: E402
from app.target_planner_service import load_planner_context, readonly_planner_session  # noqa: E402
from test_physical_naming import NOW, attach_metadata, collection, title  # noqa: E402

# SMB carries 100 ns timestamps; whole seconds keep DB stat evidence identical
# through the RO and RW CIFS views of the lab share.
EPOCH = 1_798_000_000


def put(root, relative, data, index):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.utime(path, ns=((EPOCH + index) * 10**9,) * 2)
    return path


def episode(db, owner, part, relative, number, root, index, **extra):
    path = put(root, relative, f'synthetic lab video {relative}'.encode(), index)
    info = path.stat()
    video = Video(catalog_title=part, catalog_collection=owner, relative_path=relative,
        root_folder=Path(relative).parts[0], filename=Path(relative).name, size=info.st_size,
        mtime_ns=info.st_mtime_ns, file_type='episode', content_type_manual='episode',
        season_episode_number=number, local_episode_number=number,
        episode_number_manual_override=number, episode_number_verified_at=NOW, **extra)
    db.add(video)
    return video


def subtitle(db, video, relative, root, index):
    put(root, relative, f'1\n00:00:01,000 --> 00:00:02,000\nLab {relative}\n'.encode(), index)
    row = ExternalSubtitle(relative_path=relative, codec=Path(relative).suffix.lstrip('.'))
    db.add(row)
    db.flush()
    db.add(ExternalSubtitleCompatibility(external_subtitle=row, video=video,
        status='confirmed_compatible', match_method='manual', verified_at=NOW))
    return row


def build(out):
    root, database = out / 'library', out / 'anime.db'
    root.mkdir(parents=True)
    engine = make_engine(f'sqlite:///{database}')
    index = 0
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            # Show: one MOVE, one already-canonical KEEP pair, a confirmed duplicate.
            show = collection(db, 'Show')
            show_s1 = title(db, show, metadata=False)
            attach_metadata(show_s1, 'lab-show', 'Show')
            moving = episode(db, show, show_s1, 'old/source.mkv', 1, root, index := index + 1)
            db.flush()
            episode(db, show, show_s1, 'old/copy/duplicate.mkv', 1, root, index := index + 1,
                duplicate_of_video_id=moving.id)
            # Lab Alpha: three MOVE episodes with compatible subtitles beside them.
            alpha = collection(db, 'Lab Alpha')
            alpha_s1 = title(db, alpha, metadata=False)
            attach_metadata(alpha_s1, 'lab-alpha', 'Lab Alpha')
            for number in (1, 2, 3):
                base = f'Downloads/Alpha/[Lab] Alpha - {number:02d}'
                video = episode(db, alpha, alpha_s1, base + '.mkv', number, root, index := index + 1)
                db.flush()
                subtitle(db, video, base + '.cs.srt', root, index := index + 1)
            # Lab Beta: second season out of place.
            beta = collection(db, 'Lab Beta')
            beta_s1 = title(db, beta, metadata=False)
            attach_metadata(beta_s1, 'lab-beta-1', 'Lab Beta')
            beta_s2 = title(db, beta, 'Season 2', season=2, metadata=False)
            attach_metadata(beta_s2, 'lab-beta-2', 'Lab Beta 2nd Season')
            for number in (1, 2):
                episode(db, beta, beta_s2, f'temp/beta s2/ep{number}.mkv', number, root, index := index + 1)
            keep = []
            for number in (1, 2):
                keep.append(episode(db, beta, beta_s1, f'temp/beta s1/ep{number}.mkv', number, root, index := index + 1))
            db.flush()
            # Orphan subtitle: confirmed without a video, i.e. WARNING + QUARANTINE.
            put(root, 'old/extra.cs.srt', b'confirmed subtitle without a video', index := index + 1)
            db.add(UnresolvedExternalSubtitle(relative_path='old/extra.cs.srt', filename='extra.cs.srt',
                extension='.srt', status='confirmed_no_match'))
            db.commit()
            # Season 1 of Lab Beta already sits at its canonical target (KEEP).
            targets = {r.object_id: r.target_relative_path for r in project_targets(load_planner_context(db))
                       if r.object_kind == 'video'}
            for video in keep:
                target = root / targets[video.id]
                target.parent.mkdir(parents=True, exist_ok=True)
                (root / video.relative_path).rename(target)
                video.relative_path = targets[video.id]
                video.root_folder = Path(targets[video.id]).parts[0]
                video.filename = target.name
            db.commit()
    finally:
        engine.dispose()
    # Protected and preserved content plus directories for the later E7H cleanup.
    put(root, 'IMPORT/uploading.mkv', b'untouched incoming upload', index := index + 1)
    put(root, 'seznam-souboru.txt', b'preserved root inventory', index := index + 1)
    for directory in ('Duplicates', 'Subs/empty', 'empty/unapproved', 'temp/beta s1'):
        (root / directory).mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as db:
        db.execute('PRAGMA user_version=9')
    database.chmod(0o600)
    return root, database


def verify(root, database):
    with readonly_planner_session(database) as db:
        context = load_planner_context(db)
    plan = plan_library(context, inventory_filesystem(root))
    statuses = Counter(r.status for r in plan.records)
    actions = Counter(r.action for r in plan.records)
    if (statuses.keys() - {'READY', 'WARNING'} or not statuses['WARNING'] or plan.collisions
            or not actions['MOVE'] or not actions['QUARANTINE']):
        for row in plan.records:
            if row.status != 'READY':
                print(row, file=sys.stderr)
        raise SystemExit(f'STOP: lab plan not clean: {dict(statuses)} {dict(actions)} collisions={len(plan.collisions)}')
    return dict(statuses=dict(statuses), actions=dict(actions))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.out.exists():
        raise SystemExit('STOP: output directory must not exist')
    root, database = build(args.out)
    print(verify(root, database))


if __name__ == '__main__':
    main()
