"""Confirmed ExternalTitleLink lifecycle and its single authority resolver.

A link row records that a provider identity was confirmed for a CatalogTitle;
``is_manual``/``verified_at`` stay as that historical fact.  The lifecycle
state says what the row means for the title *now*.  Only an active primary
link is metadata authority; superseded, unlinked and legacy historical rows
are preserved evidence and never supply completion, refresh, conflicts or
provider counts.  Candidate rejection is a separate axis on MetadataCandidate.
"""
from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Iterable

from sqlalchemy import and_, or_

from app.models import ExternalTitleLink

if TYPE_CHECKING:
    from app.models import CatalogTitle


class ExternalTitleLinkLifecycle(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    UNLINKED = "unlinked"
    LEGACY_HISTORICAL = "legacy_historical"


EXTERNAL_TITLE_LINK_LIFECYCLE_STATES = frozenset(ExternalTitleLinkLifecycle)


def external_title_link_is_active(link: ExternalTitleLink) -> bool:
    """Whether the link is the title's current metadata authority.

    ``NULL`` lifecycle on a primary row is the pre-lifecycle representation
    that the v6 compatibility backfill converts to ``active``; it remains an
    active fallback for rows created without the new column (test fixtures).
    """
    return bool(link.is_primary) and link.lifecycle_state in {
        None, ExternalTitleLinkLifecycle.ACTIVE,
    }


def active_primary_external_link_clause():
    """SQL form of :func:`external_title_link_is_active` for service queries."""
    return and_(
        ExternalTitleLink.is_primary.is_(True),
        or_(
            ExternalTitleLink.lifecycle_state.is_(None),
            ExternalTitleLink.lifecycle_state == ExternalTitleLinkLifecycle.ACTIVE.value,
        ),
    )


def active_primary_external_link(
    links: Iterable[ExternalTitleLink],
) -> ExternalTitleLink | None:
    return next((link for link in links if external_title_link_is_active(link)), None)


def confirmed_primary_external_link(
    title: CatalogTitle,
) -> ExternalTitleLink | None:
    """Active primary link that also carries manual verification evidence."""
    link = active_primary_external_link(title.external_links)
    if link is None or not link.is_manual or link.verified_at is None:
        return None
    return link


def activate_external_title_link(link: ExternalTitleLink) -> None:
    link.lifecycle_state = ExternalTitleLinkLifecycle.ACTIVE.value
    link.is_primary = True


def supersede_external_title_link(link: ExternalTitleLink) -> None:
    link.lifecycle_state = ExternalTitleLinkLifecycle.SUPERSEDED.value
    link.is_primary = False


def unlink_external_title_link(link: ExternalTitleLink) -> None:
    link.lifecycle_state = ExternalTitleLinkLifecycle.UNLINKED.value
    link.is_primary = False
