"""Approved grouping kinds; neither folder names nor hierarchy authority."""

from types import MappingProxyType

PHYSICAL_LAYOUT_KINDS = (
    "own_folder",
    "shared_ova",
    "shared_specials",
    "direct_season",
    "extras_openings_endings",
    "extras_promo",
    "extras_bonus",
    "extras_menus",
)

# Content-group applicability is shared by the resolver and human candidates.
# A Preview container needs the separate story-preview/Season contract.
SHARED_CONTENT_LAYOUTS = MappingProxyType({
    'ova': 'shared_ova', 'special': 'shared_specials',
    'op': 'extras_openings_endings', 'ed': 'extras_openings_endings',
    'ncop': 'extras_openings_endings', 'nced': 'extras_openings_endings',
    'preview': 'extras_promo', 'cm': 'extras_promo',
    'bonus': 'extras_bonus', 'menu': 'extras_menus',
})
