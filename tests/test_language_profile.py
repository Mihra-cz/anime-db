from datetime import datetime, timezone

import pytest

from app.catalog import build_video_language_profile, set_manual_hardsub, translation_status
from app.models import AudioTrack, ExternalSubtitle, InternalSubtitle, Video


def _video(
    *,
    audio: tuple[str, ...] = (),
    internal: tuple[str, ...] = (),
    external: tuple[str, ...] = (),
) -> Video:
    return Video(
        relative_path="Anime/Show/01.mkv",
        root_folder="Anime",
        filename="01.mkv",
        size=1,
        mtime_ns=1,
        file_type="episode",
        audio_tracks=[
            AudioTrack(stream_index=index, codec="aac", language=language)
            for index, language in enumerate(audio, 1)
        ],
        internal_subtitles=[
            InternalSubtitle(
                stream_index=index,
                codec="ass",
                language=language,
                normalized_language=language,
            )
            for index, language in enumerate(internal, 10)
        ],
        external_subtitles=[
            ExternalSubtitle(
                relative_path=f"Anime/Show/01.{index}.ass",
                codec="ass",
                language=language,
                normalized_language=language,
            )
            for index, language in enumerate(external, 1)
        ],
    )


@pytest.mark.parametrize("language", ["jpn", "ja", "japanese", "ja-JP"])
def test_japanese_audio_aliases_are_present(language):
    profile = build_video_language_profile(_video(audio=(language,)))

    assert profile.audio_languages == ("jpn",)
    assert profile.japanese_audio_status == "present"
    assert profile.has_japanese_audio is True


@pytest.mark.parametrize("audio", [("eng", "jpn"), ("jpn", "eng")])
def test_japanese_audio_presence_does_not_depend_on_track_order(audio):
    profile = build_video_language_profile(_video(audio=audio))

    assert set(profile.audio_languages) == {"eng", "jpn"}
    assert profile.japanese_audio_status == "present"
    assert profile.has_japanese_audio is True


def test_known_non_japanese_audio_is_missing():
    profile = build_video_language_profile(_video(audio=("eng",)))

    assert profile.japanese_audio_status == "missing"
    assert profile.has_japanese_audio is False


@pytest.mark.parametrize("audio", [("eng", "unknown"), ("unknown",)])
def test_unknown_audio_language_is_not_reported_as_missing(audio):
    profile = build_video_language_profile(_video(audio=audio))

    assert profile.japanese_audio_status == "unknown"
    assert profile.has_japanese_audio is False


def test_video_without_audio_has_distinct_status():
    profile = build_video_language_profile(_video())

    assert profile.audio_tracks == ()
    assert profile.audio_languages == ()
    assert profile.japanese_audio_status == "no_audio"
    assert profile.has_japanese_audio is False


@pytest.mark.parametrize(
    ("internal", "external", "language", "source"),
    [
        (("cs",), (), "cs", "internal"),
        ((), ("cs",), "cs", "external"),
        (("sk",), (), "sk", "internal"),
        ((), ("sk",), "sk", "external"),
    ],
)
def test_czech_or_slovak_subtitle_source_is_available(
    internal, external, language, source,
):
    profile = build_video_language_profile(_video(internal=internal, external=external))

    assert profile.subtitle_status == "cs_sk_available"
    assert profile.sources_by_language[language] == {source}
    assert profile.has_cs_or_sk is True
    assert profile.needs_cs_sk_subtitles is False
    assert profile.cs_sk_subtitle_priority == "none"


def test_verified_czech_hardsub_is_available():
    video = _video()
    set_manual_hardsub(
        video,
        "cs",
        verified_at=datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc),
    )

    profile = build_video_language_profile(video)

    assert profile.subtitle_status == "cs_sk_available"
    assert profile.hardsub_languages == {"cs"}
    assert profile.sources_by_language["cs"] == {"hardsub"}


def test_czech_external_and_english_internal_preserve_both_languages_and_sources():
    profile = build_video_language_profile(
        _video(internal=("eng",), external=("cs",))
    )

    assert profile.subtitle_status == "cs_sk_available"
    assert profile.has_cs is True
    assert profile.has_en is True
    assert profile.sources_by_language == {
        "eng": frozenset({"internal"}),
        "cs": frozenset({"external"}),
    }


def test_only_internal_english_is_normal_priority_fallback():
    profile = build_video_language_profile(_video(internal=("eng",)))

    assert profile.has_en is True
    assert profile.subtitle_status == "en_only"
    assert profile.needs_cs_sk_subtitles is True
    assert profile.cs_sk_subtitle_priority == "normal"


def test_video_without_cs_sk_or_internal_english_is_high_priority():
    profile = build_video_language_profile(_video())

    assert profile.subtitle_status == "no_subtitles"
    assert profile.needs_cs_sk_subtitles is True
    assert profile.cs_sk_subtitle_priority == "high"


def test_unknown_subtitle_stays_technical_and_does_not_lower_priority():
    profile = build_video_language_profile(_video(external=("unknown",)))

    assert profile.has_unknown_subtitle_language is True
    assert profile.sources_by_language["unknown"] == {"external"}
    assert profile.subtitle_status == "no_subtitles"
    assert profile.needs_cs_sk_subtitles is True
    assert profile.cs_sk_subtitle_priority == "high"


def test_external_english_is_technical_detail_not_main_english_fallback():
    profile = build_video_language_profile(_video(external=("eng",)))

    assert profile.sources_by_language["eng"] == {"external"}
    assert profile.has_en is False
    assert profile.subtitle_status == "no_subtitles"
    assert profile.cs_sk_subtitle_priority == "high"


def test_internal_and_external_czech_sources_are_both_preserved():
    profile = build_video_language_profile(
        _video(internal=("cs",), external=("cs",))
    )

    assert profile.subtitle_status == "cs_sk_available"
    assert profile.sources_by_language["cs"] == {"internal", "external"}


def test_translation_status_reuses_profile_without_changing_legacy_hardsub_meaning():
    video = _video()
    video.manual_hardsub_sk = True
    video.manual_hardsub_verified_at = None

    profile = build_video_language_profile(video)
    status = translation_status(video)

    assert profile.sources_by_language["sk"] == {"hardsub"}
    assert status.has_sk is profile.has_sk is True
    assert status.subtitle_source is None
