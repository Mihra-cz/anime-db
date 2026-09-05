"""Read-only metadata workflow state, independent of identity and content type."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from app.models import CatalogTitle, Video


SAFE_TECHNICAL_TYPES = frozenset({"op", "ed", "ncop", "nced", "menu", "cm"})
METADATA_REQUIREMENT_CHOICES = (
    ("", "Automaticky"),
    ("required", "Metadata vyžadována"),
    ("not_required", "Metadata nejsou vyžadována"),
)


def metadata_video_type(video: Video) -> str:
    # Exact physical subtype survives a broad Bonus/Special container. Any
    # explicit video override, including an unrecognized one, blocks inference.
    return (
        video.content_type_manual
        if video.content_type_manual is not None else video.file_type
    )


def has_confirmed_metadata(title: CatalogTitle) -> bool:
    return title.metadata_status == "linked_manual" and any(
        link.is_primary and link.is_manual and link.verified_at is not None
        for link in title.external_links
    )


@dataclass(frozen=True)
class MetadataCompletion:
    requirement: str
    authority: str
    state: str
    relevant: bool

    @property
    def resolved(self) -> bool:
        return self.state != "missing"

    @property
    def label(self) -> str:
        return {
            "confirmed": "Metadata potvrzena",
            "not_required": "Metadata nejsou vyžadována",
            "missing": "Metadata chybí",
        }[self.state]

    @property
    def requirement_label(self) -> str:
        source = "Ručně" if self.authority == "manual" else "Automaticky"
        value = "metadata vyžadována" if self.requirement == "required" else "metadata nejsou vyžadována"
        return f"{source}: {value}"


def resolve_metadata_completion(
    title: CatalogTitle, videos: Iterable[Video],
) -> MetadataCompletion:
    video_list = tuple(videos)
    manual = title.metadata_requirement_manual
    requirement = manual if manual in {"required", "not_required"} else (
        "not_required" if video_list and all(
            metadata_video_type(video) in SAFE_TECHNICAL_TYPES for video in video_list
        ) else "required"
    )
    state = (
        "confirmed" if has_confirmed_metadata(title)
        else "not_required" if requirement == "not_required" else "missing"
    )
    return MetadataCompletion(
        requirement, "manual" if manual is not None else "automatic",
        state, bool(video_list),
    )


def collection_metadata_ok(completions: Iterable[MetadataCompletion]) -> bool:
    relevant = [completion for completion in completions if completion.relevant]
    # Empty collections are outside the active catalog, not green completed rows.
    return bool(relevant) and all(completion.resolved for completion in relevant)


def set_metadata_requirement(title: CatalogTitle, value: str) -> None:
    if value not in dict(METADATA_REQUIREMENT_CHOICES):
        raise ValueError("Neplatný požadavek na metadata.")
    title.metadata_requirement_manual = value or None
