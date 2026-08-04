from app.catalog import classify_video, normalize_language, translation_status
from app.models import InternalSubtitle, Video


def test_normalizes_czech_variants():
    assert {normalize_language(value, None) for value in ("cs", "cze", "ces")} == {"cs"}


def test_normalizes_slovak_variants():
    assert {normalize_language(value, None) for value in ("sk", "slk", "slo")} == {"sk"}


def test_uses_english_title_when_language_is_unknown():
    assert normalize_language("unknown", "English (UK)") == "eng"
    assert normalize_language("unknown", "[Isekai]") == "unknown"


def test_internal_czech_marks_video_as_translated():
    video = Video(
        relative_path="Show/01.mkv", root_folder="Show", filename="01.mkv",
        size=1, mtime_ns=1, file_type="episode",
        internal_subtitles=[
            InternalSubtitle(stream_index=2, codec="subrip", language="cze", normalized_language="cs")
        ],
    )
    status = translation_status(video)
    assert status.has_cs
    assert status.has_cs_or_sk
    assert status.subtitle_source == "internal"


def test_classifies_bonus_video_types():
    assert classify_video("Show/NCOP 01.mkv") == "ncop"
    assert classify_video("Show/NCED 02.mkv") == "nced"
    assert classify_video("Show/OVA/Show OVA 1.mkv") == "ova"
    assert classify_video("Show/Specials/Special 01.mkv") == "special"
