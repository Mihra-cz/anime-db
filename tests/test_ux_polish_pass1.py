from pathlib import Path
from types import SimpleNamespace

import pytest

from app.catalog import catalog_title_series_label, effective_video_content_display
from app.models import CatalogTitle, Video
from app.status_presentation import (
    HIERARCHY_BADGES, METADATA_BADGES, media_collection_badge,
)


@pytest.mark.parametrize("part_type, expected", [
    ("season", "S1"),
    ("ova", "OVA · S1"),
    ("film", "Film · S1"),
])
def test_structural_label_distinguishes_type_from_season_context(part_type, expected):
    title = CatalogTitle(
        local_title="Part", normalized_local_title="part",
        relative_root_path="Anime/Part", part_type=part_type, season_number=1,
    )
    assert catalog_title_series_label(title) == expected


@pytest.mark.parametrize("file_type, manual, label", [
    ("episode", None, "Epizoda"),
    ("other", None, "Jiné"),
    ("episode", "other", "Jiné · ručně zařazeno"),
])
def test_content_type_uses_same_label_regardless_of_authority(file_type, manual, label):
    video = Video(
        relative_path="Anime/opaque.mkv", root_folder="Anime",
        filename="opaque.mkv", size=1, mtime_ns=1,
        file_type=file_type, content_type_manual=manual,
    )
    assert effective_video_content_display(video).display_label == label
    assert video.file_type == file_type


def test_badges_keep_verified_review_conflict_and_information_distinct():
    assert HIERARCHY_BADGES["verified"].severity == "verified"
    assert HIERARCHY_BADGES["automatic"].severity == "success"
    assert HIERARCHY_BADGES["review_required"].severity == "warning"
    assert HIERARCHY_BADGES["conflict"].severity == "error"
    assert METADATA_BADGES["not_required"].severity == "info"
    assert METADATA_BADGES["missing"].severity == "warning"


def test_media_badge_uses_canonical_evaluation_severity_and_duplicate_scope():
    evaluations = [
        SimpleNamespace(completion_required=True, subtitle_severity="success", audio_severity="success"),
        SimpleNamespace(completion_required=False, subtitle_severity="error", audio_severity="error"),
    ]
    assert media_collection_badge(evaluations).label == "Média OK"
    evaluations.append(SimpleNamespace(
        completion_required=True, subtitle_severity="warning", audio_severity="success",
    ))
    assert media_collection_badge(evaluations).severity == "warning"


def test_catalog_markup_prioritizes_title_and_links_to_workflows():
    row = Path("app/templates/_catalog_workbench_row.html").read_text()
    css = Path("app/static/style.css").read_text()
    assert 'class="catalog-title-link"' in row
    assert 'data-label="Hierarchie"' in row
    assert 'data-label="Média"' in row
    assert 'data-label="Metadata"' in row
    assert 'href="/hierarchy-review/{{ group.catalog_collection_id }}"' in row
    assert 'href="/media-check?q={{ group.name|urlencode }}"' in row
    assert '.catalog-workbench th:first-child, .catalog-workbench td:first-child { width: 55%; }' in css
    assert 'class="catalog-row-details"' in row


def test_hidden_and_bulk_controls_have_progressive_disclosure_safety():
    css = Path("app/static/style.css").read_text()
    fields = Path("app/static/hierarchy_fields.js").read_text()
    media = Path("app/templates/media_edit.html").read_text()
    media_bulk = Path("app/templates/_media_bulk_editor.html").read_text()
    dirty = Path("app/static/local_edit_dirty.js").read_text()
    confirmation = Path("app/templates/edit_confirmation.html").read_text()
    hierarchy = Path("app/templates/hierarchy_review.html").read_text()
    assert '[hidden] { display: none !important; }' in css
    assert 'input:disabled, select:disabled, textarea:disabled' in css
    assert 'field.hidden = !visible' in fields
    assert 'input.disabled = !visible' in fields
    assert 'data-selected-count aria-live="polite"' in media_bulk
    assert 'data-media-bulk-form' in media_bulk
    assert "bulk_editor('media-title-bulk'" in media
    assert "'/media-check/titles/' ~ catalog_title.id ~ '/bulk-edit'" in media
    assert 'data-edit-form' in media
    assert 'button.disabled = !changed' in dirty
    assert "Opravdu chcete uložit tyto změny?" in confirmation
    assert 'id="empty-collection-count" aria-live="polite"' in hierarchy
    assert 'window.confirm(' in hierarchy
