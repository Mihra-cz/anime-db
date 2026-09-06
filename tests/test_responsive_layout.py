from pathlib import Path
import re

from app.main import templates


TEMPLATE_ROOT = Path("app/templates")
STYLE_PATH = Path("app/static/style.css")


def source(name: str) -> str:
    return (TEMPLATE_ROOT / name).read_text(encoding="utf-8")


def test_all_jinja_templates_load_and_base_declares_responsive_shell():
    names = sorted(
        templates.env.list_templates(filter_func=lambda name: name.endswith(".html"))
    )

    assert {
        "base.html", "index.html", "catalog.html", "metadata_review.html",
        "_metadata_requirement.html",
    } <= set(names)
    for name in names:
        templates.env.get_template(name)

    base = source("base.html")
    assert 'name="viewport" content="width=device-width, initial-scale=1"' in base
    assert 'class="site-header"' in base
    assert 'class="primary-nav"' in base
    assert 'class="{% block main_class %}content-shell{% endblock %}"' in base


def test_every_data_table_uses_local_scroll_and_compact_card_hook():
    table_templates = {
        path.name: source(path.name)
        for path in TEMPLATE_ROOT.glob("*.html")
        if "<table" in source(path.name)
    }

    assert table_templates
    for name, template_source in table_templates.items():
        assert 'class="table-wrap"' in template_source, name
        table_tags = re.findall(r"<table[^>]*>", template_source)
        assert table_tags, name
        assert all('class="responsive-cards' in tag for tag in table_tags), name


def test_responsive_css_uses_content_breakpoints_without_global_scaling():
    css = STYLE_PATH.read_text(encoding="utf-8")

    assert "--content-standard: 80rem" in css
    assert "--content-wide: 120rem" in css
    assert "@media (max-width: 56.25rem)" in css
    assert "@media (max-width: 38.75rem)" in css
    assert ".responsive-cards td::before" in css
    assert "overflow-x: auto" in css
    assert "textarea" in css and "width: 100%" in css
    assert "transform: scale" not in css
    assert not re.search(r"(?:^|[;{])\s*zoom\s*:", css)


def test_catalog_thumbnails_are_fixed_decorative_and_responsive():
    css = STYLE_PATH.read_text(encoding="utf-8")
    thumbnail = source("_artwork_thumbnail.html")
    index = source("index.html")
    catalog = source("catalog.html")
    collection = source("collection.html")

    cell_rule = re.search(r"\.artwork-title-cell \{([^}]*)\}", css).group(1)
    thumbnail_rule = re.search(r"\.artwork-thumbnail \{([^}]*)\}", css).group(1)
    image_rule = re.search(r"\.artwork-thumbnail img \{([^}]*)\}", css).group(1)
    copy_rule = re.search(r"\.artwork-title-copy \{([^}]*)\}", css).group(1)
    assert "display: flex" in cell_rule
    assert "min-width: 0" in cell_rule
    assert "max-width: 100%" in cell_rule
    assert "flex: 0 0 2.75rem" in thumbnail_rule
    assert "width: 2.75rem" in thumbnail_rule
    assert "aspect-ratio: 2 / 3" in thumbnail_rule
    assert "overflow: hidden" in thumbnail_rule
    assert "object-fit: cover" in image_rule
    assert "width: 100%" in image_rule and "height: 100%" in image_rule
    assert "min-width: 0" in copy_rule
    assert 'aria-hidden="true"' in thumbnail
    assert 'alt=""' in thumbnail
    assert 'onerror="this.hidden=true"' in thumbnail
    assert "artwork_thumbnail(row.thumbnail_url)" in index
    assert "artwork_thumbnail(thumbnail_url)" in catalog
    assert "artwork_thumbnail(item.thumbnail_url)" in collection
    assert "item.show_artwork" in collection
    assert "primary_cover_artwork" not in collection
    assert "artwork_type" not in collection


def test_editable_video_table_uses_its_own_landscape_card_breakpoint():
    css = STYLE_PATH.read_text(encoding="utf-8")
    series = source("series.html")

    assert '@media (max-width: 75rem)' in css
    assert '.episode-table td { overflow-wrap: normal; word-break: normal; }' in css
    assert '.episode-table .compact-column { white-space: nowrap; }' in css
    assert '.episode-table td[data-label="Audio"] .tag' in css
    assert '.episode-table .episode-number .inline-form > input[type="number"]' in css
    assert '.table-wrap:has(> .episode-table)' in css
    assert 'class="responsive-cards episode-table"' in series
    for label in ("Série", "Epizoda", "Délka", "Hardsub", "Typ", "Rozlišení", "Audio"):
        assert f'data-label="{label}"' in series


def test_media_check_reuses_landscape_cards_and_stacks_filters_on_mobile():
    css = STYLE_PATH.read_text(encoding="utf-8")
    media_check = source("media_check.html")

    assert 'class="responsive-cards episode-table media-check-table"' in media_check
    assert 'class="media-check-summary"' in media_check
    assert 'class="panel media-check-filters"' in media_check
    assert '.media-check-filters { grid-template-columns: 1fr; }' in css
    assert '.media-control-grid' in css
    assert "language_display_label(language, include_name=true)" in media_check
    assert "profile.audio_languages|map('upper')" not in media_check
    assert "select, textarea" in css
    assert "max-width: 100%;" in css


def test_unassigned_video_workflow_uses_desktop_cards_without_horizontal_scroll():
    css = STYLE_PATH.read_text(encoding="utf-8")
    root_videos = source("root_videos.html")

    assert 'class="responsive-cards root-video-table"' in root_videos
    assert "@media (max-width: 120rem)" in css
    assert ".root-video-table tbody tr" in css
    assert ".table-wrap:has(> .root-video-table) { overflow-x: visible" in css
    assert 'class="unassigned-title-form"' in root_videos


def test_touch_accessible_paths_and_critical_hierarchy_controls_remain_present():
    series = source("series.html")
    root_videos = source("root_videos.html")
    hierarchy = source("hierarchy_review_detail.html")

    for template_source in (series, root_videos):
        assert 'class="technical-details"' in template_source
        assert "Technická cesta" in template_source
        assert "{{ video.relative_path }}" in template_source

    assert 'id="active-review-issues"' in hierarchy
    assert 'id="manual-split"' in hierarchy
    assert 'class="inline-form part-type-form manual-hierarchy-form"' in hierarchy
    assert "effective_video_content_display" in hierarchy
    assert "video_content_type_choices" in hierarchy


def test_hierarchy_all_anime_index_uses_native_disclosure_and_wrapping_rows():
    overview = source("hierarchy_review.html")
    css = STYLE_PATH.read_text(encoding="utf-8")

    assert '<details class="panel all-collections-index" id="all-collections">' in overview
    assert "<summary>Všechna anime ({{ all_collection_rows|length }})</summary>" in overview
    assert 'aria-controls="all-collections-list"' in overview
    assert 'aria-live="polite"' in overview
    row_rule = re.search(r"\.all-collections-row \{([^}]*)\}", css).group(1)
    link_rule = re.search(r"\.all-collections-row > a \{([^}]*)\}", css).group(1)
    badge_rule = re.search(
        r"\.all-collections-row > \.status-badge \{([^}]*)\}", css
    ).group(1)
    assert "justify-content: flex-start" in row_rule
    assert "justify-content: space-between" not in row_rule
    assert "flex-wrap: wrap" in row_rule
    assert "column-gap: .625rem" in row_rule
    assert "flex: 0 1 auto" in link_rule
    assert "max-width: 100%" in link_rule
    assert "overflow-wrap: anywhere" in link_rule
    assert "margin-left: auto" not in row_rule + link_rule + badge_rule
    assert "max-width: 100%" in badge_rule
