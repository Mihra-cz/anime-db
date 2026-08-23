from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

from .models import Video
from .numbering import is_nonprimary_duplicate_video


MEDIA_PART_NUMBER_ERROR = "Část média musí být kladné celé číslo."


def validate_media_part_number(value: int | None) -> None:
    """Validate the authoritative video-level physical segment ordinal."""
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, int) or value < 1
    ):
        raise ValueError(MEDIA_PART_NUMBER_ERROR)


def set_media_part_number(video: Video, value: int | None) -> Video:
    """Set or clear Media Part without touching hierarchy, numbering or metadata."""
    validate_media_part_number(value)
    video.media_part_number = value
    return video


def active_media_part_videos(videos: Iterable[Video]) -> tuple[Video, ...]:
    """Return valid primary segments; confirmed secondary copies are not parts."""
    return tuple(
        video for video in videos
        if (
            isinstance(video.media_part_number, int)
            and not isinstance(video.media_part_number, bool)
            and video.media_part_number >= 1
            and not is_nonprimary_duplicate_video(video)
        )
    )


def media_part_total(videos: Iterable[Video]) -> int | None:
    """Return N only for a complete distinct 1..N set with at least two parts."""
    active = active_media_part_videos(videos)
    counts = Counter(video.media_part_number for video in active)
    if any(count > 1 for count in counts.values()):
        return None
    ordinals = set(counts)
    if len(ordinals) < 2:
        return None
    maximum = max(ordinals)
    return maximum if ordinals == set(range(1, maximum + 1)) else None


def media_part_label(video: Video, sibling_videos: Iterable[Video]) -> str | None:
    """Build a human-facing label without conflating Media Part and hierarchy Part."""
    number = video.media_part_number
    if (
        not isinstance(number, int)
        or isinstance(number, bool)
        or number < 1
    ):
        return None
    total = media_part_total(sibling_videos)
    return f"Část média {number}/{total}" if total is not None else f"Část média {number}"


def duplicate_media_part_ordinals(videos: Iterable[Video]) -> tuple[int, ...]:
    """Find ordinals used by multiple active primary videos of one CatalogTitle."""
    counts = Counter(
        video.media_part_number for video in active_media_part_videos(videos)
    )
    return tuple(sorted(number for number, count in counts.items() if count > 1))


def media_part_ordinal_warning(
    video: Video, sibling_videos: Iterable[Video],
) -> str | None:
    number = video.media_part_number
    if number not in duplicate_media_part_ordinals(sibling_videos):
        return None
    return (
        f"Číslo části média {number} používá více aktivních primárních videí "
        "tohoto titulu."
    )


def media_part_sequence_warning(videos: Iterable[Video]) -> str | None:
    """Describe a non-contiguous active set without creating a hierarchy reason."""
    siblings = tuple(videos)
    ordinals = sorted({
        video.media_part_number for video in active_media_part_videos(siblings)
    })
    if duplicate_media_part_ordinals(siblings):
        return None
    if not ordinals or ordinals == [1] or media_part_total(siblings) is not None:
        return None
    values = ", ".join(str(number) for number in ordinals)
    return (
        f"Části média netvoří souvislou sadu od 1 (nalezeno: {values}). "
        "Jde o neblokující video-level upozornění."
    )


def media_part_summary_label(videos: Iterable[Video]) -> str | None:
    total = media_part_total(videos)
    if total is None:
        return None
    noun = "části média" if 2 <= total <= 4 else "částí média"
    return f"{total} {noun}"
