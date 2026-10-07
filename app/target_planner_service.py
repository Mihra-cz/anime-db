"""Constant-query evidence preload and SQLite read-only entrypoint.

No startup/migration/scanner lifecycle is imported or run. The clean-session
boundary prevents unflushed edits becoming accidental planner authority.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
import sqlite3
from urllib.parse import quote

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, raiseload
from sqlalchemy.orm.attributes import set_committed_value

from .catalog import detect_episode_number, effective_video_content_type
from .collection_presentation import build_collection_presentation, title_has_authoritative_season_context
from .hierarchy_authority import manual_hierarchy_authority_state, structural_hierarchy_issue, split_season_structure_issues
from .models import (
    CatalogCollection, CatalogTitle, Video, ExternalTitleLink, TitleMetadata,
    PhysicalNamingChoice, PhysicalLayoutChoice, VideoVariantGroup,
    ExternalSubtitle, ExternalSubtitleCompatibility, UnresolvedExternalSubtitle,
)
from .numbering import duplicate_relation_state, effective_video_numbering, unresolved_duplicate_groups
from .physical_naming_service import physical_naming_context_from_models
from .physical_layout_service import physical_layout_context_from_models
from .supplementary import supplementary_review_issues
from .target_planner_types import PlannerContext, SourceCollection, TargetTitle, TargetVideo, TargetSubtitle, UnmatchedSubtitle
from .target_planner import technical_root


@contextmanager
def readonly_planner_session(database_path: Path):
    """SQLite mode=ro at the file boundary; no production mkdir or migration."""
    path = Path(database_path).absolute()
    uri = 'file:' + quote(str(path), safe='/') + '?mode=ro'
    engine = create_engine('sqlite://', creator=lambda: sqlite3.connect(uri, uri=True))
    try:
        with Session(engine, autoflush=False, expire_on_commit=False) as session:
            # Start after SQLAlchemy's initial dialect setup/rollback. BEGIN
            # is transaction control; mode=ro forbids a database write.
            session.connection().connection.driver_connection.execute('BEGIN')
            yield session
    finally:
        engine.dispose()


def load_planner_context(session: Session) -> PlannerContext:
    """Eleven whole-table SELECTs at any scale, then shared in-memory resolvers.

    ORM relationship hydration is loader state, not business mutation. Raiseload
    makes any omitted relationship fail instead of introducing a per-row query.
    """
    if session.new or session.dirty or session.deleted:
        raise ValueError('Target Planner requires a clean read-only session.')
    def rows(model):
        return list(session.scalars(select(model).options(raiseload('*')).order_by(*model.__mapper__.primary_key)))
    with session.no_autoflush:
        collections = rows(CatalogCollection)
        titles = rows(CatalogTitle)
        videos = rows(Video)
        links = rows(ExternalTitleLink)
        metadata = rows(TitleMetadata)
        naming_choices = rows(PhysicalNamingChoice)
        layout_choices = rows(PhysicalLayoutChoice)
        variants = rows(VideoVariantGroup)
        subtitles = rows(ExternalSubtitle)
        edges = rows(ExternalSubtitleCompatibility)
        unmatched = rows(UnresolvedExternalSubtitle)
        collections_by_id = {c.id: c for c in collections}
        titles_by_id = {t.id: t for t in titles}
        videos_by_id = {v.id: v for v in videos}
        variants_by_id = {v.id: v for v in variants}
        titles_by_collection = defaultdict(list)
        videos_by_collection = defaultdict(list)
        videos_by_title = defaultdict(list)
        links_by_title = defaultdict(list)
        metadata_by_title = {m.catalog_title_id: m for m in metadata}
        for t in titles:
            titles_by_collection[t.catalog_collection_id].append(t)
        for v in videos:
            videos_by_collection[v.catalog_collection_id].append(v)
            videos_by_title[v.catalog_title_id].append(v)
        for link in links:
            links_by_title[link.catalog_title_id].append(link)
        for collection in collections:
            set_committed_value(collection, 'titles', titles_by_collection[collection.id])
            set_committed_value(collection, 'videos', videos_by_collection[collection.id])
        for title in titles:
            set_committed_value(title, 'collection', collections_by_id.get(title.catalog_collection_id))
            set_committed_value(title, 'videos', videos_by_title[title.id])
            set_committed_value(title, 'external_links', links_by_title[title.id])
            set_committed_value(title, 'metadata_record', metadata_by_title.get(title.id))
        for video in videos:
            set_committed_value(video, 'catalog_title', titles_by_id.get(video.catalog_title_id))
            set_committed_value(video, 'catalog_collection', collections_by_id.get(video.catalog_collection_id))
            set_committed_value(video, 'duplicate_of', videos_by_id.get(video.duplicate_of_video_id))
            set_committed_value(video, 'video_variant_group', variants_by_id.get(video.video_variant_group_id))
        naming = physical_naming_context_from_models(collections, titles, naming_choices)
        layout = physical_layout_context_from_models(titles, layout_choices)
        title_issues = defaultdict(set)
        video_issues = defaultdict(set)
        presentations = {}
        for collection in collections:
            members = videos_by_collection[collection.id]
            siblings = titles_by_collection[collection.id]
            presentations[collection.id] = build_collection_presentation(siblings, include_videos=False)
            for issue in split_season_structure_issues(siblings):
                for title in issue.titles:
                    title_issues[title.id].add('ambiguous_split_season')
            for issue in supplementary_review_issues(members, collection_scope=True, titles_by_id=titles_by_id):
                for video in issue.videos:
                    video_issues[video.id].add(issue.code)
            for group in unresolved_duplicate_groups(members):
                for video in group.videos:
                    video_issues[video.id].add('unresolved_duplicate_identity')
        target_titles = []
        for title in titles:
            if manual_hierarchy_authority_state(title) == 'incomplete':
                title_issues[title.id].add('incomplete_manual_snapshot')
            structural = structural_hierarchy_issue(title.effective_part_type, title.effective_season_number, title.effective_part_number)
            if structural:
                title_issues[title.id].add('invalid_structural_hierarchy')
            target_titles.append(TargetTitle(title.id, title.catalog_collection_id, title.effective_part_type,
                title.effective_season_number, title.effective_part_number, title.relative_root_path, tuple(sorted(title_issues[title.id]))))
        target_videos = []
        for video in videos:
            title = titles_by_id.get(video.catalog_title_id)
            detection = detect_episode_number(video.filename)
            numbering = effective_video_numbering(video, title, use_current_title=False, detection=detection)
            content = effective_video_content_type(video, title, use_current_title=False, detection=detection)
            group = variants_by_id.get(video.video_variant_group_id)
            if video.video_variant_group_id is not None and (group is None or group.catalog_title_id != video.catalog_title_id):
                video_issues[video.id].add('variant_owner_conflict')
            if content == 'recap' and not title_has_authoritative_season_context(title, presentation=presentations.get(video.catalog_collection_id), titles=titles_by_collection[video.catalog_collection_id]):
                video_issues[video.id].add('recap_season_unavailable')
            if video.duplicate_status_manual == 'suspected':
                video_issues[video.id].add('manual_duplicate_suspected')
            validity = duplicate_relation_state(video, known_videos=videos_by_id, allow_relationship_load=False)
            target_videos.append(TargetVideo(video.id, video.catalog_title_id, video.catalog_collection_id, video.relative_path,
                content, numbering.season_episode_number if content == 'episode' else None,
                numbering.supplementary_number if content != 'recap' else None,
                numbering.supplementary_number if content == 'recap' else None, video.media_part_number,
                video.video_variant_group_id, group.manual_label if group else None, group.release_source if group else None,
                video.duplicate_of_video_id, validity.value if validity else None, tuple(sorted(video_issues[video.id])),
                source_evidence_filename=video.filename))
        edges_by_asset = defaultdict(list)
        for edge in edges:
            edges_by_asset[edge.external_subtitle_id].append((edge.video_id, edge.status))
        source_collections = []
        for collection in collections:
            # Alternate current roots are explicit DB locators of member rows.
            # This index only associates owner-less assets, never Video owners.
            locators = {p for p in (collection.relative_root_path,
                *(v.root_folder for v in videos_by_collection[collection.id])) if p and not technical_root(p)}
            source_collections.append(SourceCollection(collection.id, tuple(sorted(locators))))
        result = PlannerContext(naming, layout, tuple(source_collections), tuple(target_titles), tuple(target_videos),
            tuple(TargetSubtitle(s.id, s.relative_path, tuple(sorted(edges_by_asset[s.id]))) for s in subtitles),
            tuple(UnmatchedSubtitle(s.id, s.relative_path, s.status, s.filename) for s in unmatched))
    if session.new or session.dirty or session.deleted:
        raise RuntimeError('Target Planner mutated its read-only session.')
    return result
