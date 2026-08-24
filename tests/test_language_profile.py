from datetime import datetime, timezone

import pytest

from app.catalog import (
    build_video_language_profile,
    effective_audio_track_language,
    effective_external_subtitle_language,
    normalize_language,
    set_audio_track_manual_language,
    set_external_subtitle_manual_language,
    set_manual_hardsub,
    translation_status,
)
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

    assert profile.audio_languages == ("ja",)
    assert profile.audio_status == "japanese"
    assert profile.has_japanese_audio is True


@pytest.mark.parametrize("audio", [("eng", "jpn"), ("jpn", "eng")])
def test_japanese_audio_presence_does_not_depend_on_track_order(audio):
    profile = build_video_language_profile(_video(audio=audio))

    assert set(profile.audio_languages) == {"en", "ja"}
    assert profile.audio_status == "japanese"
    assert profile.has_japanese_audio is True


def test_english_audio_is_informational_english_only():
    profile = build_video_language_profile(_video(audio=("eng",)))

    assert profile.audio_status == "english_only"
    assert profile.has_japanese_audio is False


@pytest.mark.parametrize("audio", [("eng", "unknown"), ("unknown",)])
def test_unknown_audio_language_is_not_reported_as_missing(audio):
    profile = build_video_language_profile(_video(audio=audio))

    assert profile.audio_status == "unknown"
    assert profile.has_japanese_audio is False


def test_video_without_audio_has_distinct_status():
    profile = build_video_language_profile(_video())

    assert profile.audio_tracks == ()
    assert profile.audio_languages == ()
    assert profile.audio_status == "no_audio"
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

    assert profile.subtitle_status == "preferred"
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

    assert profile.subtitle_status == "preferred"
    assert profile.hardsub_languages == {"cs"}
    assert profile.sources_by_language["cs"] == {"hardsub"}


def test_czech_external_and_english_internal_preserve_both_languages_and_sources():
    profile = build_video_language_profile(
        _video(internal=("eng",), external=("cs",))
    )

    assert profile.subtitle_status == "preferred"
    assert profile.has_cs is True
    assert profile.has_en is True
    assert profile.sources_by_language == {
        "en": frozenset({"internal"}),
        "cs": frozenset({"external"}),
    }


def test_only_internal_english_is_normal_priority_fallback():
    profile = build_video_language_profile(_video(internal=("eng",)))

    assert profile.has_en is True
    assert profile.subtitle_status == "fallback_internal_en"
    assert profile.has_internal_english_subtitles is True
    assert profile.needs_cs_sk_subtitles is True
    assert profile.cs_sk_subtitle_priority == "normal"


def test_video_without_cs_sk_or_internal_english_is_high_priority():
    profile = build_video_language_profile(_video())

    assert profile.subtitle_status == "missing"
    assert profile.needs_cs_sk_subtitles is True
    assert profile.cs_sk_subtitle_priority == "high"


def test_unknown_subtitle_stays_technical_and_does_not_lower_priority():
    profile = build_video_language_profile(_video(external=("unknown",)))

    assert profile.has_unknown_subtitle_language is True
    assert profile.sources_by_language["unknown"] == {"external"}
    assert profile.subtitle_status == "missing"
    assert profile.needs_cs_sk_subtitles is True
    assert profile.cs_sk_subtitle_priority == "high"


def test_external_english_is_technical_detail_not_main_english_fallback():
    profile = build_video_language_profile(_video(external=("eng",)))

    assert profile.sources_by_language["en"] == {"external"}
    assert profile.has_en is False
    assert profile.subtitle_status == "missing"
    assert profile.cs_sk_subtitle_priority == "high"


def test_internal_and_external_czech_sources_are_both_preserved():
    profile = build_video_language_profile(
        _video(internal=("cs",), external=("cs",))
    )

    assert profile.subtitle_status == "preferred"
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


@pytest.mark.parametrize(
    ("aliases", "expected"),
    [
        (("cs", "cze", "ces"), "cs"),
        (("sk", "slo", "slk"), "sk"),
        (("en", "eng"), "en"),
        (("ja", "jpn"), "ja"),
    ],
)
def test_shared_language_normalization_uses_canonical_codes(aliases, expected):
    assert {normalize_language(alias) for alias in aliases} == {expected}


def test_unknown_language_stays_unknown():
    assert normalize_language("unknown") == "unknown"


@pytest.mark.parametrize("audio", [("en", "en"), ("eng", "en")])
def test_all_english_audio_tracks_are_english_only(audio):
    profile = build_video_language_profile(_video(audio=audio))

    assert profile.audio_status == "english_only"
    assert profile.has_japanese_audio is False


@pytest.mark.parametrize("audio", [("de",), ("en", "de")])
def test_known_non_japanese_non_english_audio_is_other_known(audio):
    profile = build_video_language_profile(_video(audio=audio))

    assert profile.audio_status == "other_known"
    assert profile.has_japanese_audio is False


def test_unknown_audio_has_priority_over_known_non_japanese_audio():
    profile = build_video_language_profile(_video(audio=("en", "unknown")))

    assert profile.audio_status == "unknown"


def test_japanese_audio_has_priority_over_unknown_audio():
    profile = build_video_language_profile(_video(audio=("ja", "unknown")))

    assert profile.audio_status == "japanese"


@pytest.mark.parametrize(
    ("detected", "manual", "expected_status"),
    [
        ("unknown", "ja", "japanese"),
        ("unknown", "en", "english_only"),
        ("en", "ja", "japanese"),
    ],
)
def test_audio_manual_language_controls_effective_evaluation(
    detected, manual, expected_status,
):
    video = _video(audio=(detected,))
    track = video.audio_tracks[0]

    set_audio_track_manual_language(track, manual)
    profile = build_video_language_profile(video)

    assert track.language == detected
    assert track.manual_language == manual
    assert effective_audio_track_language(track) == manual
    assert profile.audio_status == expected_status


def test_clearing_audio_manual_language_restores_detected_language():
    video = _video(audio=("en",))
    track = video.audio_tracks[0]
    set_audio_track_manual_language(track, "ja")

    set_audio_track_manual_language(track, "")
    profile = build_video_language_profile(video)

    assert track.manual_language is None
    assert effective_audio_track_language(track) == "en"
    assert profile.audio_status == "english_only"


def test_unknown_external_manual_czech_override_is_preferred():
    video = _video(external=("unknown",))
    subtitle = video.external_subtitles[0]

    set_external_subtitle_manual_language(subtitle, "cze")
    profile = build_video_language_profile(video)

    assert subtitle.normalized_language == "unknown"
    assert subtitle.manual_language == "cs"
    assert effective_external_subtitle_language(subtitle) == "cs"
    assert profile.subtitle_status == "preferred"
    assert profile.sources_by_language["cs"] == {"external"}


def test_automatic_external_english_manual_slovak_override_is_preferred():
    video = _video(external=("eng",))
    subtitle = video.external_subtitles[0]

    set_external_subtitle_manual_language(subtitle, "slk")
    profile = build_video_language_profile(video)

    assert subtitle.normalized_language == "eng"
    assert effective_external_subtitle_language(subtitle) == "sk"
    assert profile.subtitle_status == "preferred"
    assert profile.has_sk is True


def test_clearing_external_manual_language_restores_automatic_language():
    video = _video(external=("eng",))
    subtitle = video.external_subtitles[0]
    set_external_subtitle_manual_language(subtitle, "sk")

    set_external_subtitle_manual_language(subtitle, "")
    profile = build_video_language_profile(video)

    assert subtitle.manual_language is None
    assert effective_external_subtitle_language(subtitle) == "en"
    assert profile.subtitle_status == "missing"


def test_internal_czech_wins_over_unknown_external_subtitle():
    profile = build_video_language_profile(
        _video(internal=("cs",), external=("unknown",))
    )

    assert profile.subtitle_status == "preferred"
    assert profile.has_unknown_subtitle_language is True


def test_external_english_and_internal_unknown_are_still_missing():
    profile = build_video_language_profile(
        _video(internal=("unknown",), external=("eng",))
    )

    assert profile.subtitle_status == "missing"
    assert profile.has_internal_english_subtitles is False


def test_unreviewed_hardsub_without_language_is_not_preferred():
    video = _video()

    profile = build_video_language_profile(video)

    assert profile.hardsub_languages == frozenset()
    assert profile.subtitle_status == "missing"
