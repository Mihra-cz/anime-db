"""Shared batch projection and server validation for explicit layout decisions."""
import hmac

from sqlalchemy import or_, select
from sqlalchemy.orm import joinedload, selectinload

from .layout_review import LayoutEvidence, LayoutReviewContext
from .models import CatalogCollection, CatalogTitle, Video
from .naming_review_service import naming_review_context_from_models
from .physical_layout_service import (
    physical_layout_context_from_models, confirm_physical_layout_choice,
    reconfirm_physical_layout_choice, reset_physical_layout_choice,
)


class StaleLayoutForm(ValueError):
    pass


def layout_review_context_from_models(collections, titles, *, layout_choices=None):
    collections, titles = tuple(collections), tuple(titles)
    naming = naming_review_context_from_models(collections, titles)
    if layout_choices is None:
        layout_choices = [t.physical_layout_choice for t in titles if t.physical_layout_choice is not None]
    physical = physical_layout_context_from_models(titles, layout_choices)
    evidence = {}
    labels = {c.id: c.local_title for c in collections}
    for title in titles:
        videos = tuple(title.videos)
        confirmed = naming.physical.titles[title.id]
        romaji = confirmed.romaji if confirmed.metadata_identity else None
        evidence[title.id] = LayoutEvidence(title.id, title.local_title,
            labels.get(title.catalog_collection_id, 'Bez kolekce'), romaji, len(videos),
            sum(v.media_part_number is not None for v in videos),
            len({v.video_variant_group_id for v in videos if v.video_variant_group_id is not None}),
            sum(v.duplicate_of_video_id is not None for v in videos))
    return LayoutReviewContext(physical, naming, evidence)


def load_layout_review_context(session, *, collection_ids=None, title_ids=()):
    """Four SELECT batches shared by the Names and Layout tabs, without flush."""
    collections_query = select(CatalogCollection).options(
        joinedload(CatalogCollection.physical_naming_choice), joinedload(CatalogCollection.titles),
    )
    titles_query = select(CatalogTitle).options(
        joinedload(CatalogTitle.collection), joinedload(CatalogTitle.metadata_record),
        joinedload(CatalogTitle.physical_naming_choice), joinedload(CatalogTitle.physical_layout_choice),
        selectinload(CatalogTitle.external_links),
        selectinload(CatalogTitle.videos).joinedload(Video.catalog_title).joinedload(CatalogTitle.collection),
        selectinload(CatalogTitle.videos).joinedload(Video.duplicate_of).joinedload(Video.catalog_title).joinedload(CatalogTitle.collection),
    )
    if collection_ids is not None:
        ids = tuple(collection_ids)
        collections_query = collections_query.where(CatalogCollection.id.in_(ids))
        titles_query = titles_query.where(or_(CatalogTitle.catalog_collection_id.in_(ids), CatalogTitle.id.in_(tuple(title_ids))))
    with session.no_autoflush:
        collections = session.scalars(collections_query).unique().all()
        titles = session.scalars(titles_query).unique().all()
        return layout_review_context_from_models(collections, titles)


def apply_layout_review_decision(session, owner, index, *, action, fingerprint, candidate_key=''):
    unit = index.units.get(owner.id)
    if unit is None:
        raise ValueError('Tato část není vlastníkem fyzického rozložení.')
    if not isinstance(fingerprint, str) or not fingerprint.isascii() or not hmac.compare_digest(unit.fingerprint, fingerprint):
        raise StaleLayoutForm('Kontext fyzického rozložení se změnil. Načti aktuální kandidáty a potvrď volbu znovu.')
    if action == 'reset':
        return reset_physical_layout_choice(session, owner)
    if action == 'reconfirm':
        choice = unit.resolution.choice
        candidate_key = choice.layout_kind if choice else None
    elif action != 'save':
        raise ValueError('Neznámá akce rozložení.')
    candidate = next((c for c in unit.candidates if c.layout_kind == candidate_key and c.valid), None)
    if candidate is None:
        raise ValueError('Vyber aktuálně použitelné rozložení. Nejprve vyřeš případné závislosti.')
    if action == 'reconfirm':
        return reconfirm_physical_layout_choice(session, owner)
    return confirm_physical_layout_choice(session, owner, candidate.layout_kind)
