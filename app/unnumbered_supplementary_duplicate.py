"""Narrow human authority for physical copies with no supplementary ordinal."""

from __future__ import annotations

from .catalog import effective_video_content_type
from .models import Video
from .supplementary import ORDINAL_TYPES, supplementary_ordinal, typed_structural_contexts


UNNUMBERED_SUPPLEMENTARY_SAME_CONTENT = "unnumbered_supplementary_same_content"


def validate_unnumbered_same_content_members(videos: list[Video]) -> str:
    """Validate current evidence; no filename or row-order identity inference."""
    if len(videos) < 2 or len(videos) != len(set(videos)):
        raise ValueError("Vyberte alespoň dvě různá videa.")
    titles = [video.__dict__.get("catalog_title") for video in videos]
    if any(title is None for title in titles):
        raise ValueError("Část videa není načtená nebo určená.")
    title = titles[0]
    if title.id is None or any(
        video.catalog_title_id != title.id or current.id != title.id
        for video, current in zip(videos, titles)
    ):
        raise ValueError("Všechna videa musí patřit do stejného CatalogTitle.")
    collection = title.__dict__.get("collection")
    if collection is None or collection.id is None or any(
        video.catalog_collection_id != collection.id
        or current.catalog_collection_id != collection.id
        for video, current in zip(videos, titles)
    ):
        raise ValueError("Všechna videa musí patřit do stejné collection.")
    types = {
        effective_video_content_type(video, title, use_current_title=False)
        for video in videos
    }
    if len(types) != 1 or not types <= (ORDINAL_TYPES - {"recap"}):
        raise ValueError("Všechna videa musí mít stejný supplementary typ mimo Recap.")
    kind = next(iter(types))
    if any(
        (ordinal := supplementary_ordinal(video, title, use_current_title=False)) is None
        or ordinal.supplementary_type != kind or ordinal.number is not None
        for video in videos
    ):
        raise ValueError("Všechna videa musí mít neurčený supplementary ordinal.")
    if any(video.media_part_number is not None for video in videos):
        raise ValueError("Media Parts nelze potvrdit jako duplicitní fyzické kopie.")
    if len({video.video_variant_group_id for video in videos}) != 1:
        raise ValueError("Variant groups nejsou shodné.")
    contexts = typed_structural_contexts(videos, list(collection.titles))
    if len({contexts[video].key for video in videos}) != 1:
        raise ValueError("Videa nemají stejný authoritative structural context.")
    return kind
