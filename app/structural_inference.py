from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from .models import CatalogCollection, CatalogTitle, Video
from .numbering import effective_video_numbering, is_nonprimary_duplicate_video


SOFT_LONG_FLAT_SEQUENCE_WARNING_TEMPLATE = (
    "Delší souvislá řada {episode_range} bez explicitního dělení. Zkontrolujte "
    "případné rozdělení na sezóny nebo části."
)
LONG_FLAT_SEQUENCE_REVIEW_REASON = (
    "Neobvykle dlouhá souvislá řada bez explicitního dělení. Ověřte, zda nejde "
    "o více sezón nebo částí."
)
GENERIC_TITLE_REVIEW_REASON = (
    "Typ části nelze bezpečně určit. Zvolte konkrétní strukturální typ."
)


@dataclass(frozen=True)
class DirectRootEpisodeProfile:
    standard_count: int
    episode_min: int | None
    episode_max: int | None
    contiguous_from_one: bool
    unresolved_duplicate_numbers: tuple[int, ...]

    @property
    def supports_automatic_season_one(self) -> bool:
        # Jeden soubor může být neoznačený film, OVA nebo special. Dvě a více
        # skutečných standardních epizod tvoří minimální epizodický důkaz.
        return self.standard_count >= 2 and self.contiguous_from_one


@dataclass(frozen=True)
class AutomaticStructuralValues:
    part_type: str
    season_number: int | None
    part_number: int | None
    season_label: str | None
    reason: str


def is_direct_root_title(title: CatalogTitle) -> bool:
    return bool(
        title.collection is not None
        and title.relative_root_path == title.collection.relative_root_path
    )


def direct_root_episode_profile(videos: list[Video]) -> DirectRootEpisodeProfile:
    numbers = [
        state.numbering_input
        for video in videos
        if not is_nonprimary_duplicate_video(video)
        and (state := effective_video_numbering(video)).is_standard
        and state.detection.season_hint in {None, 1}
        and state.numbering_input is not None
    ]
    counts = Counter(numbers)
    duplicates = tuple(sorted(number for number, count in counts.items() if count > 1))
    unique = sorted(counts)
    episode_min = unique[0] if unique else None
    episode_max = unique[-1] if unique else None
    contiguous = bool(
        unique
        and not duplicates
        and episode_min == 1
        and unique == list(range(1, episode_max + 1))
    )
    return DirectRootEpisodeProfile(
        standard_count=len(numbers),
        episode_min=episode_min,
        episode_max=episode_max,
        contiguous_from_one=contiguous,
        unresolved_duplicate_numbers=duplicates,
    )


def infer_automatic_structural_values(
    *, part_type: str, season_number: int | None, part_number: int | None,
    season_label: str | None,
    is_direct_root: bool, videos: list[Video],
) -> AutomaticStructuralValues:
    """Return automatic structural fields without creating manual authority."""
    if not is_direct_root or part_type not in {"title", "season"}:
        return AutomaticStructuralValues(
            part_type, season_number, part_number, season_label,
            "explicit_structural_type",
        )
    profile = direct_root_episode_profile(videos)
    if profile.supports_automatic_season_one:
        return AutomaticStructuralValues(
            "season", 1, None, "S1", "direct_root_contiguous_episode_sequence"
        )
    return AutomaticStructuralValues(
        "title", None, None, None, "direct_root_without_safe_episode_sequence"
    )


def apply_automatic_structural_inference(
    collection: CatalogCollection,
) -> bool:
    """Refresh automatic fields while preserving every manual hierarchy value."""
    changed = False
    for title in collection.titles:
        if title.hierarchy_manual_override:
            continue
        values = infer_automatic_structural_values(
            part_type=title.part_type or "title",
            season_number=title.season_number,
            part_number=title.part_number,
            season_label=title.season_label,
            is_direct_root=is_direct_root_title(title),
            videos=list(title.videos),
        )
        current = (
            title.part_type, title.season_number, title.part_number,
            title.season_label,
        )
        inferred = (
            values.part_type, values.season_number, values.part_number,
            values.season_label,
        )
        if current != inferred:
            (
                title.part_type, title.season_number, title.part_number,
                title.season_label,
            ) = inferred
            changed = True
    return changed


def automatic_flat_sequence_notice(
    title: CatalogTitle, videos: list[Video] | None = None,
) -> str | None:
    """Return a derived non-blocking notice; it is intentionally not persisted."""
    if (
        title.hierarchy_manual_override
        or not is_direct_root_title(title)
        or title.effective_part_type not in {"title", "season"}
    ):
        return None
    profile = direct_root_episode_profile(
        list(title.videos) if videos is None else videos
    )
    if (
        profile.contiguous_from_one
        and 15 <= profile.standard_count <= 24
    ):
        return SOFT_LONG_FLAT_SEQUENCE_WARNING_TEMPLATE.format(
            episode_range=f"E{profile.episode_min}–E{profile.episode_max}",
        )
    return None


def has_long_flat_sequence_requiring_review(
    title: CatalogTitle, videos: list[Video] | None = None,
) -> bool:
    if (
        title.hierarchy_manual_override
        or not is_direct_root_title(title)
        or title.effective_part_type not in {"title", "season"}
    ):
        return False
    profile = direct_root_episode_profile(
        list(title.videos) if videos is None else videos
    )
    return profile.contiguous_from_one and profile.standard_count > 24
