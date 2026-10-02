"""Logical title references and a non-authoritative locator candidate index."""
from dataclasses import dataclass
from collections.abc import Iterable, Mapping, Sequence

from .models import CatalogTitle


@dataclass(frozen=True)
class ExistingTitleRef:
    title_id: int


@dataclass(frozen=True)
class PlannedTitleRef:
    token: int


TitleRef = ExistingTitleRef | PlannedTitleRef


def title_ref_key(ref: TitleRef) -> tuple[int, int]:
    return (0, ref.title_id) if isinstance(ref, ExistingTitleRef) else (1, ref.token)


class TitleLocatorIndex:
    """A locator has zero or more candidates; IDs retain every owner."""
    def __init__(self, titles: Iterable[CatalogTitle] = ()):
        self.by_id: dict[int, CatalogTitle] = {}
        self._by_locator: dict[str, dict[int, CatalogTitle]] = {}
        self._locator_by_id: dict[int, str] = {}
        for title in titles:
            self.add(title)

    def add(self, title: CatalogTitle) -> None:
        if title.id is None:
            raise ValueError("Locator indexing requires a persisted owner ID.")
        if title.id in self.by_id:
            self.discard(title)
        self.by_id[title.id] = title
        self._locator_by_id[title.id] = title.relative_root_path
        self._by_locator.setdefault(title.relative_root_path, {})[title.id] = title

    def discard(self, title: CatalogTitle) -> None:
        self.by_id.pop(title.id, None)
        locator = self._locator_by_id.pop(title.id, title.relative_root_path)
        owners = self._by_locator.get(locator, {})
        owners.pop(title.id, None)
        if not owners:
            self._by_locator.pop(locator, None)

    def candidates(self, locator: str) -> tuple[CatalogTitle, ...]:
        return tuple(t for _, t in sorted(self._by_locator.get(locator, {}).items()))

    def unique(self, locator: str) -> CatalogTitle | None:
        candidates = self.candidates(locator)
        return candidates[0] if len(candidates) == 1 else None


def locator_candidates(
    index: TitleLocatorIndex | Mapping[str, Sequence[CatalogTitle]], locator: str,
) -> tuple[CatalogTitle, ...]:
    if isinstance(index, TitleLocatorIndex):
        return index.candidates(locator)
    return tuple(index.get(locator, ()))
