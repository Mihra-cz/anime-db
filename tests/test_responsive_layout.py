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

    assert len(names) == 16
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
