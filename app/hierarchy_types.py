from __future__ import annotations


# Ordered UI choices and backend validation intentionally share one definition.
# ``film`` belongs to CatalogTitle hierarchy, not to per-video content types.
PART_TYPE_CHOICES: tuple[tuple[str, str], ...] = (
    ("season", "Sezóna"),
    ("part", "Part"),
    ("cour", "Cour"),
    ("film", "Film"),
    ("ova", "OVA"),
    ("special", "Special"),
    ("preview", "Preview"),
    ("recap", "Recap"),
    ("bonus", "Bonus"),
    ("other", "Other"),
    ("title", "Titul"),
)
PART_TYPES = frozenset(value for value, _ in PART_TYPE_CHOICES)
PART_TYPE_LABELS = dict(PART_TYPE_CHOICES)

VIDEO_CONTENT_TYPE_CHOICES: tuple[tuple[str, str], ...] = (
    ("recap", "Recap"),
    ("preview", "Preview"),
    ("special", "Special"),
    ("ova", "OVA"),
    ("bonus", "Bonus"),
    ("other", "Other"),
)
VIDEO_CONTENT_TYPES = frozenset(value for value, _ in VIDEO_CONTENT_TYPE_CHOICES)
