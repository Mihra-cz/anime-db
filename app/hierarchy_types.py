from __future__ import annotations


# Ordered UI choices and backend validation intentionally share one definition.
# ``film`` belongs to CatalogTitle hierarchy, not to per-video content types.
PART_TYPE_CHOICES: tuple[tuple[str, str], ...] = (
    ("season", "Sezóna"),
    ("part", "Part"),
    ("film", "Film"),
    ("ova", "OVA"),
    ("special", "Special"),
    ("preview", "Preview"),
    ("recap", "Recap"),
    ("bonus", "Bonus"),
    ("other", "Other"),
)
# ``cour`` remains accepted for persisted legacy rows and old technical JSON,
# but it is not offered as a new user-facing hierarchy choice.
PART_TYPES = frozenset({*(value for value, _ in PART_TYPE_CHOICES), "cour"})
PART_TYPE_LABELS = {**dict(PART_TYPE_CHOICES), "cour": "Cour"}
# ``title`` remains readable as a legacy/technical inference fallback, but new
# authoritative hierarchy input must always choose a concrete structural type.
TECHNICAL_PART_TYPES = frozenset({*PART_TYPES, "title"})

VIDEO_CONTENT_TYPE_CHOICES: tuple[tuple[str, str], ...] = (
    ("recap", "Recap"),
    ("preview", "Preview"),
    ("special", "Special"),
    ("ova", "OVA"),
    ("bonus", "Bonus"),
    ("other", "Other"),
)
VIDEO_CONTENT_TYPES = frozenset(value for value, _ in VIDEO_CONTENT_TYPE_CHOICES)
