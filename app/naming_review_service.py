"""Batch review loading and validated explicit decisions; callers own transactions."""
from __future__ import annotations

import hmac
import json
from pathlib import PurePosixPath

from sqlalchemy import or_, select
from sqlalchemy.orm import joinedload, selectinload

from .catalog import effective_video_content_type
from .models import CatalogCollection, CatalogTitle
from .numbering import effective_video_numbering
from .physical_naming_components import sanitize_component
from .physical_naming_formatters import (
    EpisodeIdentity, SupplementaryIdentity, format_episode_component,
    format_supplementary_component,
)
from .physical_naming_service import (
    confirm_physical_naming_choice, physical_naming_context_from_models,
    reconfirm_physical_naming_choice, reset_physical_naming_choice,
)
from .naming_review import (
    KnownNamingFile, NamingCollectionEvidence, NamingReviewContext,
    NamingTitleEvidence, make_naming_candidate,
)


class StaleNamingForm(ValueError):
    pass


def _known_files(title):
    files = []
    deferred = 0
    probe_prefix = sanitize_component('x')
    for video in title.videos:
        numbering = effective_video_numbering(video, title, use_current_title=False)
        content_type = effective_video_content_type(video, title, use_current_title=False)
        season, part = title.effective_season_number, title.effective_part_number
        if content_type == 'episode' and numbering.season_episode_number is not None and season is not None:
            identity = EpisodeIdentity(season, numbering.season_episode_number, part, video.media_part_number)
        elif content_type in {'ova', 'special', 'preview', 'op', 'ed', 'ncop', 'nced', 'recap'}:
            if content_type == 'recap' and (season is None or numbering.supplementary_number is None):
                deferred += 1
                continue
            identity = SupplementaryIdentity(
                content_type, season, part,
                ordinal=numbering.supplementary_number if content_type != 'recap' else None,
                recap_position=numbering.supplementary_number if content_type == 'recap' else None,
                media_part=video.media_part_number,
            )
        else:
            # Unsupported grammar or missing identity is a separate domain's
            # work. Naming does not invent numbers or classify those files.
            deferred += 1
            continue
        extension = PurePosixPath(video.filename).suffix
        formatter = format_episode_component if isinstance(identity, EpisodeIdentity) else format_supplementary_component
        # An unresolved structural/numbering/extension value is not invalid
        # human text. Defer its unknown filename budget rather than requiring
        # a naming decision that cannot repair another domain's authority.
        if not formatter(probe_prefix, identity, extension).valid:
            deferred += 1
            continue
        files.append(KnownNamingFile(video.id, identity, extension))
    return tuple(files), deferred


def naming_review_context_from_models(collections, titles, choices=None):
    """Project an eager ORM graph, including a v6 read-only adapter with choices=[]."""
    collections, titles = tuple(collections), tuple(titles)
    if choices is None:
        choices = [owner.physical_naming_choice for owner in (*collections, *titles)
                   if owner.physical_naming_choice is not None]
    physical = physical_naming_context_from_models(collections, titles, choices)
    evidence = {}
    for title in titles:
        metadata = title.metadata_record
        confirmed = physical.titles[title.id]
        english, synonyms = None, ()
        if confirmed.metadata_identity and 'metadata_payload_mismatch' not in confirmed.metadata_diagnostics and metadata is not None:
            english = metadata.title_english
            try:
                values = json.loads(metadata.synonyms_json)
                synonyms = tuple(value for value in values if isinstance(value, str)) if isinstance(values, list) else ()
            except (ValueError, TypeError):
                pass
        files, deferred = _known_files(title)
        evidence[title.id] = NamingTitleEvidence(
            title.id, title.local_title, title.manual_display_title, english, synonyms, files, deferred,
        )
    return NamingReviewContext(physical, {
        owner.id: NamingCollectionEvidence(owner.id, owner.local_title, owner.manual_display_title)
        for owner in collections
    }, evidence)


def load_naming_review_context(session, *, collection_ids=None, title_ids=()):
    """Four bounded SELECTs; no per-owner/candidate lazy lookup or flush."""
    collection_query = select(CatalogCollection).options(joinedload(CatalogCollection.physical_naming_choice))
    title_query = select(CatalogTitle).options(
        joinedload(CatalogTitle.metadata_record), joinedload(CatalogTitle.physical_naming_choice),
        selectinload(CatalogTitle.external_links), selectinload(CatalogTitle.videos),
    )
    if collection_ids is not None:
        ids = tuple(collection_ids)
        collection_query = collection_query.where(CatalogCollection.id.in_(ids))
        title_query = title_query.where(or_(CatalogTitle.catalog_collection_id.in_(ids), CatalogTitle.id.in_(tuple(title_ids))))
    with session.no_autoflush:
        collections = session.scalars(collection_query).all()
        titles = session.scalars(title_query).all()
        return naming_review_context_from_models(collections, titles)


def validate_naming_fingerprint(unit, fingerprint):
    if not isinstance(fingerprint, str) or not fingerprint.isascii() or not hmac.compare_digest(unit.fingerprint, fingerprint):
        raise StaleNamingForm('Kontext pojmenování se změnil. Načti aktuální kandidáty a potvrď volbu znovu.')


def apply_naming_review_decision(session, owner, index, *, action, fingerprint, candidate_key='', custom_text=''):
    """Use only current server candidates and the existing physical naming service."""
    scope = 'collection' if isinstance(owner, CatalogCollection) else 'title'
    key = (scope, owner.id)
    unit = index.units[key]
    validate_naming_fingerprint(unit, fingerprint)
    if action == 'reset':
        return reset_physical_naming_choice(session, owner)
    if action == 'reconfirm':
        if unit.resolution.choice is None or unit.effective is None or not unit.effective.can_confirm:
            raise ValueError('Tento název nelze znovu potvrdit. Vyber použitelný název.')
        return reconfirm_physical_naming_choice(session, owner)
    if action != 'save':
        raise ValueError('Neznámá naming akce.')
    if unit.resolution.authority == 'inherited':
        raise ValueError('Zděděný prefix se potvrzuje u nadřazené naming položky.')
    candidate = (make_naming_candidate(index.context, key, custom_text, 'custom')
                 if candidate_key == 'custom' else next((c for c in unit.candidates if c.key == candidate_key), None))
    if candidate is None:
        raise ValueError('Vyber aktuální kandidát nebo vlastní název.')
    if not candidate.can_confirm:
        raise ValueError(' '.join(candidate.diagnostic_messages) or 'Název nelze potvrdit.')
    if candidate.key == 'saved':
        return reconfirm_physical_naming_choice(session, owner)
    return confirm_physical_naming_choice(
        session, owner, candidate.raw_text, candidate.kind,
        source_title_id=candidate.source_title_id if candidate.kind == 'parent_prefix' else None,
    )
