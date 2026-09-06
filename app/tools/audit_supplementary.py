"""Read-only supplementary inventory. Run with python -m app.tools.audit_supplementary DB."""
from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, joinedload, raiseload

from app.catalog import detect_episode_number
from app.models import Video
from app.supplementary import supplementary_inventory, supplementary_ordinal, variant_group_id


def audit(database: Path) -> dict:
    path = database.resolve()
    before = {'size': path.stat().st_size, 'mtime_ns': path.stat().st_mtime_ns,
              'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    engine = create_engine('sqlite://', creator=lambda: sqlite3.connect(
        f'{path.as_uri()}?mode=ro', uri=True,
    ))
    with Session(engine, autoflush=False) as session:
        videos = list(session.scalars(select(Video).options(
            joinedload(Video.catalog_title), joinedload(Video.video_variant_group), raiseload('*'),
        ).order_by(Video.id)))
        grouped = defaultdict(list)
        rows = []
        for video in videos:
            detection = detect_episode_number(video.filename)
            state = supplementary_ordinal(video, detection=detection)
            if state is None:
                continue
            subtype = state.supplementary_type
            # PV is the persisted file_type for both Preview and PV; retain the
            # shared namespace while making source spelling visible in the table.
            bucket = 'pv' if subtype == 'preview' and not re.search(r'\bpreview', video.filename, re.I) else subtype
            group = video.video_variant_group
            row = dict(id=video.id, path=video.relative_path, raw_file_type=video.file_type,
                       raw_supplementary_number=detection.supplementary_number,
                       manual_number=video.episode_number_manual_override,
                       manual_type=video.content_type_manual, effective_type=subtype,
                       effective_ordinal=state.number, source=state.source,
                       media_part=video.media_part_number,
                       variant_group=variant_group_id(video),
                       variant_lane=group.manual_label if group else None,
                       content_variant=group.content_variant if group else None,
                       catalog_title_id=video.catalog_title_id,
                       catalog_title=video.catalog_title.local_title if video.catalog_title else None,
                       duplicate_of=video.duplicate_of_video_id)
            rows.append(row)
            grouped[(video.catalog_title_id, subtype)].append((video, row, bucket))
        totals = {kind: dict(physical=0, raw_parser_ordinal=0, manual_numbering=0,
                            effective_ordinal=0, unknown=0, collisions=0,
                            media_part_groups=0, variant_groups=0)
                  for kind in ('op', 'ed', 'ncop', 'nced', 'ova', 'special', 'preview', 'pv', 'cm')}
        candidates, collisions = [], []
        variant_ids = defaultdict(set)
        for (title_id, subtype), members in grouped.items():
            inventory = supplementary_inventory([v for v, _, _ in members])
            bucket = members[0][2]
            for video, row, source_bucket in members:
                total = totals[source_bucket]
                total['physical'] += 1
                total['raw_parser_ordinal'] += row['raw_supplementary_number'] is not None
                total['manual_numbering'] += row['manual_number'] is not None
                total['effective_ordinal'] += row['effective_ordinal'] is not None
                total['unknown'] += row['effective_ordinal'] is None
                if row['variant_group'] is not None:
                    variant_ids[source_bucket].add(row['variant_group'])
            for partition in inventory.partitions:
                if partition.requires_review:
                    totals[bucket]['collisions'] += 1
                    collisions.append(dict(title_id=title_id, type=subtype, ordinal=partition.identity.ordinal,
                                           video_ids=[v.id for v in partition.videos]))
                totals[bucket]['media_part_groups'] += len({variant_group_id(v) for v in partition.videos if v.media_part_number is not None})
            if any(v.media_part_number is not None for v in inventory.unknown_videos):
                totals[bucket]['media_part_groups'] += 1
            unknown_rows = [r for _, r, _ in members if r['effective_ordinal'] is None]
            if len(members) > 1 and unknown_rows:
                candidates.append(dict(title_id=title_id, title=members[0][1]['catalog_title'],
                                       type=subtype, physical=len(members), unknown=unknown_rows))
        for kind, ids in variant_ids.items():
            totals[kind]['variant_groups'] = len(ids)
        result = dict(database=before, total_videos=len(videos), totals=totals,
                      unknown_total=sum(t['unknown'] for t in totals.values()),
                      collision_candidates=candidates, ordinal_collisions=collisions,
                      nande=[r for r in rows if 'nande koko' in r['path'].casefold()],
                      rows=rows)
    engine.dispose()
    after = {'size': path.stat().st_size, 'mtime_ns': path.stat().st_mtime_ns,
             'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    result['database_unchanged_during_audit'] = before == after
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    print(json.dumps(audit(parser.parse_args().database), ensure_ascii=False, indent=2))
