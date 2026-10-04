"""Fresh dc91a8d legacy observations, pinned independently of canonical grammar."""
from dataclasses import asdict

import pytest

from app.catalog import classify_video, detect_episode_number


@pytest.mark.parametrize("stem,fields,file_type", [
    ("Title 01", {"kind": "standard", "number": 1}, "episode"),
    ("Title 02", {"kind": "standard", "number": 2}, "episode"),
    ("Title 22", {"kind": "standard", "number": 22}, "episode"),
    ("22", {"kind": "unknown"}, "other"),
    ("022", {"kind": "standard", "number": 22}, "episode"),
    ("00", {"kind": "zero", "number": 0}, "other"),
    ("Title 00", {"kind": "zero", "number": 0}, "other"),
    ("14.5", {"kind": "unknown"}, "other"),
    ("Title 14.5", {"kind": "fractional", "number": 14, "fraction": "5"}, "other"),
    ("E01", {"kind": "standard", "number": 1}, "episode"),
    ("EP02", {"kind": "standard", "number": 2}, "episode"),
    ("Episode 10", {"kind": "standard", "number": 10}, "episode"),
    ("01v2", {"kind": "standard", "number": 1}, "episode"),
    ("Title - 01", {"kind": "standard", "number": 1}, "episode"),
    ("Title01", {"kind": "unknown"}, "other"),
    ("Title!01", {"kind": "standard", "number": 1}, "episode"),
    ("S01E01-Title", {"kind": "standard", "number": 1, "season_hint": 1, "filename_episode_hint": 1, "title_candidate": "Title"}, "episode"),
    ("S1E2 Title", {"kind": "standard", "number": 2, "season_hint": 1, "filename_episode_hint": 2, "title_candidate": "Title"}, "episode"),
    ("S01E05.5", {"kind": "fractional", "number": 5, "fraction": "5", "season_hint": 1, "filename_episode_hint": 5}, "other"),
    ("S01E14 [SP]", {"kind": "supplementary", "supplementary_type": "special", "season_hint": 1, "filename_episode_hint": 14}, "special"),
    ("Show - S01P02E03", {"kind": "unknown"}, "other"),
    ("Show - S01P02E03-MP01", {"kind": "unknown"}, "other"),
    ("Show - S01E03-MP01", {"kind": "standard", "number": 3, "season_hint": 1, "filename_episode_hint": 3, "title_candidate": "MP01"}, "episode"),
    ("Show - S02 - OVA 01", {"kind": "supplementary", "number": 1, "supplementary_type": "ova", "context_hint": "Show - S02"}, "ova"),
])
def test_legacy_detection_and_classification_remain_at_baseline(stem, fields, file_type):
    expected = dict.fromkeys((
        "number", "fraction", "supplementary_type", "context_hint", "season_hint",
        "filename_episode_hint", "title_candidate", "version_hint", "structural_marker",
    ))
    expected.update(fields)
    filename = stem + ".mkv"
    assert asdict(detect_episode_number(filename)) == expected
    assert classify_video(filename) == file_type
