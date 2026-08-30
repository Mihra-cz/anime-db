from __future__ import annotations

from datetime import datetime
from typing import cast

from .models import CatalogTitle, Video, VideoVariantGroup, utc_now


VIDEO_VARIANT_RELEASE_SOURCE_CHOICES: tuple[tuple[str, str], ...] = (
    ("tv", "TV"),
    ("bd", "BD"),
    ("web", "WEB"),
    ("dvd", "DVD"),
    ("other", "Other"),
)
VIDEO_VARIANT_RELEASE_SOURCES = frozenset(
    value for value, _label in VIDEO_VARIANT_RELEASE_SOURCE_CHOICES
)
VIDEO_VARIANT_CONTENT_VARIANT_CHOICES: tuple[tuple[str, str], ...] = (
    ("censored", "Censored"),
    ("uncensored", "Uncensored"),
    ("other", "Other"),
)
VIDEO_VARIANT_CONTENT_VARIANTS = frozenset(
    value for value, _label in VIDEO_VARIANT_CONTENT_VARIANT_CHOICES
)


_UNSPECIFIED_GROUP = object()
_UNSPECIFIED_TITLE = object()


def _normalize_manual_label(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("Označení video variant group nesmí být prázdné.")
    return normalized


def _normalize_taxonomy_value(
    value: str | None,
    supported: frozenset[str],
    *,
    field_label: str,
) -> str | None:
    normalized = value.strip().casefold() if value is not None else ""
    if not normalized:
        return None
    if normalized not in supported:
        raise ValueError(f"Neplatná hodnota {field_label}.")
    return normalized


def _titles_match(first: CatalogTitle | None, second: CatalogTitle | None) -> bool:
    if first is None or second is None:
        return first is second
    if first is second:
        return True
    return first.id is not None and first.id == second.id


def _require_persisted_title(title: CatalogTitle | None) -> CatalogTitle:
    if title is None or title.id is None:
        raise ValueError("Video variant group musí patřit existujícímu CatalogTitle.")
    return title


def validate_video_variant_group(group: VideoVariantGroup) -> None:
    """Validate the persisted manual authority fields without changing the group."""
    if group.id is None:
        raise ValueError("Video variant group musí být před přiřazením uložená.")
    _require_persisted_title(group.catalog_title)
    _normalize_manual_label(group.manual_label)
    _normalize_taxonomy_value(
        group.release_source,
        VIDEO_VARIANT_RELEASE_SOURCES,
        field_label="release source",
    )
    _normalize_taxonomy_value(
        group.content_variant,
        VIDEO_VARIANT_CONTENT_VARIANTS,
        field_label="content variant",
    )


def create_video_variant_group(
    catalog_title: CatalogTitle,
    *,
    manual_label: str,
    release_source: str | None = None,
    content_variant: str | None = None,
    note: str | None = None,
    verified_at: datetime | None = None,
) -> VideoVariantGroup:
    """Build one explicit manual-authority lane for an already stored title."""
    _require_persisted_title(catalog_title)
    return VideoVariantGroup(
        catalog_title=catalog_title,
        manual_label=_normalize_manual_label(manual_label),
        release_source=_normalize_taxonomy_value(
            release_source,
            VIDEO_VARIANT_RELEASE_SOURCES,
            field_label="release source",
        ),
        content_variant=_normalize_taxonomy_value(
            content_variant,
            VIDEO_VARIANT_CONTENT_VARIANTS,
            field_label="content variant",
        ),
        note=(note or "").strip() or None,
        verified_at=verified_at or utc_now(),
    )


def update_video_variant_group(
    group: VideoVariantGroup,
    *,
    manual_label: str,
    release_source: str | None = None,
    content_variant: str | None = None,
    note: str | None = None,
    verified_at: datetime | None = None,
) -> None:
    """Update mutable classification fields while preserving the stable group ID."""
    validate_video_variant_group(group)
    normalized_label = _normalize_manual_label(manual_label)
    normalized_source = _normalize_taxonomy_value(
        release_source,
        VIDEO_VARIANT_RELEASE_SOURCES,
        field_label="release source",
    )
    normalized_content = _normalize_taxonomy_value(
        content_variant,
        VIDEO_VARIANT_CONTENT_VARIANTS,
        field_label="content variant",
    )
    group.manual_label = normalized_label
    group.release_source = normalized_source
    group.content_variant = normalized_content
    group.note = (note or "").strip() or None
    group.verified_at = verified_at or utc_now()


def validate_video_variant_assignment(
    video: Video,
    group: VideoVariantGroup,
    *,
    catalog_title: CatalogTitle | None | object = _UNSPECIFIED_TITLE,
) -> None:
    """Reject a cross-title assignment; never repairs or guesses a target group."""
    validate_video_variant_group(group)
    title = (
        video.catalog_title
        if catalog_title is _UNSPECIFIED_TITLE
        else cast(CatalogTitle | None, catalog_title)
    )
    if title is None or not _titles_match(title, group.catalog_title):
        raise ValueError(
            "Video lze přiřadit pouze k variant group ze stejného CatalogTitle."
        )


def assign_video_variant_group(
    video: Video,
    group: VideoVariantGroup | None,
) -> None:
    """Apply or clear a manually confirmed group assignment."""
    if group is not None:
        validate_video_variant_assignment(video, group)
    video.video_variant_group = group


def assign_video_catalog_title(
    video: Video,
    catalog_title: CatalogTitle | None,
    *,
    video_variant_group: VideoVariantGroup | None | object = _UNSPECIFIED_GROUP,
) -> None:
    """Set title membership and atomically keep, replace, or clear variant authority.

    Without an explicit new group, an existing assignment survives only when it
    belongs to the target title. No corresponding group is inferred or cloned.
    """
    if video_variant_group is _UNSPECIFIED_GROUP:
        current_group = video.video_variant_group
        target_group = (
            current_group
            if current_group is not None
            and _titles_match(catalog_title, current_group.catalog_title)
            else None
        )
    else:
        target_group = cast(VideoVariantGroup | None, video_variant_group)
        if target_group is not None:
            validate_video_variant_assignment(
                video,
                target_group,
                catalog_title=catalog_title,
            )

    video.catalog_title = catalog_title
    video.video_variant_group = target_group
