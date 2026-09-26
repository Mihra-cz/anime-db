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
from sqlalchemy.orm import Session, joinedload, raiseload, selectinload

from app.catalog import detect_episode_number
from app.models import CatalogCollection, CatalogTitle, Video
from app.supplementary import (
    ORDINAL_TYPES, supplementary_inventory, supplementary_ordinal,
    supplementary_review_issues, typed_structural_contexts, variant_group_id,
)


def audit(database: Path) -> dict:
    path = database.resolve()
    before = {'size': path.stat().st_size, 'mtime_ns': path.stat().st_mtime_ns,
              'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    engine = create_engine('sqlite://', creator=lambda: sqlite3.connect(
        f'{path.as_uri()}?mode=ro', uri=True,
    ))
    with Session(engine, autoflush=False) as session:
        videos = list(session.scalars(select(Video).options(
            joinedload(Video.catalog_title).joinedload(CatalogTitle.collection)
            .selectinload(CatalogCollection.titles),
            joinedload(Video.video_variant_group), selectinload(Video.duplicate_of),
            raiseload('*'),
        ).order_by(Video.id)))
        typed, rows = [], []
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
            typed.append((video, row, bucket))
        # Same scope as Hierarchy Review: typed identities of supplementary
        # titles attached to one main part share a collection-wide namespace.
        typed_videos = [video for video, _, _ in typed]
        contexts = typed_structural_contexts(typed_videos)
        grouped = defaultdict(list)
        for video, row, bucket in typed:
            grouped[(contexts[video], row['effective_type'])].append((video, row, bucket))
        totals = {kind: dict(physical=0, raw_parser_ordinal=0, manual_numbering=0,
                            effective_ordinal=0, unknown=0, collisions=0,
                            media_part_groups=0, variant_groups=0)
                  for kind in sorted(ORDINAL_TYPES | {'pv'})}
        variant_ids = defaultdict(set)
        for members in grouped.values():
            inventory = supplementary_inventory(
                [v for v, _, _ in members], collection_scope=True, contexts=contexts,
            )
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
                totals[bucket]['media_part_groups'] += len({variant_group_id(v) for v in partition.videos if v.media_part_number is not None})
            if any(v.media_part_number is not None for v in inventory.unknown_videos):
                totals[bucket]['media_part_groups'] += 1
        for kind, ids in variant_ids.items():
            totals[kind]['variant_groups'] = len(ids)
        # Candidates and collisions are the shared review read model, which
        # counts logical identities (confirmed copies, lanes, Media Parts),
        # never physical rows.
        rows_by_id = {row['id']: row for row in rows}
        candidates, collisions, broken = [], [], []
        for issue in supplementary_review_issues(typed_videos, collection_scope=True):
            context = contexts[issue.videos[0]]
            item = dict(context=context.label, type=issue.supplementary_type,
                        title_ids=sorted({v.catalog_title_id for v in issue.videos}))
            if issue.code == 'missing_supplementary_ordinal':
                candidates.append(dict(item, physical=len(grouped[(context, issue.supplementary_type)]),
                                       unknown=[rows_by_id[v.id] for v in issue.videos]))
            elif issue.code == 'supplementary_ordinal_collision':
                collisions.append(dict(item, ordinal=issue.known_ordinals[0],
                                       video_ids=[v.id for v in issue.videos]))
            else:
                broken.append(dict(item, video_ids=[v.id for v in issue.videos]))
        result = dict(database=before, total_videos=len(videos), totals=totals,
                      unknown_total=sum(t['unknown'] for t in totals.values()),
                      collision_candidates=candidates, ordinal_collisions=collisions,
                      broken_identities=broken,
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
