from pathlib import Path

from app.subtitles import (
    detect_language, safe_subtitle_matches, subtitle_match_kind, subtitle_matches,
)


def test_subtitle_pairing():
    video = Path("Episode 01.mkv")
    assert subtitle_matches(video, Path("Episode 01.srt"))
    assert subtitle_matches(video, Path("Episode 01.cs.ass"))
    assert not subtitle_matches(video, Path("Episode 02.srt"))
    assert not subtitle_matches(video, Path("Episode 01.release.ass"))


def test_fractional_exact_match_wins_and_numeric_suffix_is_not_language():
    subtitle = Path("Title - 05.5.ass")
    method, candidates = safe_subtitle_matches(
        [Path("Title - 05.mkv"), Path("Title - 05.5.mkv")], subtitle,
    )
    assert method == "exact"
    assert candidates == (Path("Title - 05.5.mkv"),)

    method, candidates = safe_subtitle_matches([Path("Title - 05.mkv")], subtitle)
    assert method is None
    assert candidates == ()


def test_language_suffix_is_allowlisted_and_still_requires_unique_candidate():
    subtitle = Path("Title - 01.cs.ass")
    method, candidates = safe_subtitle_matches([Path("Title - 01.mkv")], subtitle)
    assert method == "language_suffix"
    assert candidates == (Path("Title - 01.mkv"),)

    method, candidates = safe_subtitle_matches(
        [Path("Title - 01.mkv"), Path("Title - 01.mp4")], subtitle,
    )
    assert method == "language_suffix"
    assert len(candidates) == 2
    assert subtitle_matches(Path("Title - 01.MKV"), Path("Title - 01.CS.ASS"))


def test_detects_czech_and_slovak():
    assert detect_language("Ahoj, jsem tady. Když přijdeš, řeknu ti něco, protože můžu.") == "cs"
    assert detect_language("Ahoj, som tu. Keď prídeš, poviem ti niečo, pretože môžem.") == "sk"


def test_cz_is_a_safe_filename_suffix_alias_beside_existing_ones():
    video = Path("Series - 01.mkv")
    for suffix in ("cz", "cs", "cze", "ces", "sk"):
        subtitle = Path(f"Series - 01.{suffix}.ass")
        assert subtitle_match_kind(video, subtitle) == "language_suffix"
        assert safe_subtitle_matches([video], subtitle) == (
            "language_suffix", (video,),
        )
    unknown_suffix = Path("Series - 01.foo.ass")
    assert subtitle_match_kind(video, unknown_suffix) is None
    assert safe_subtitle_matches([video], unknown_suffix) == (None, ())


def test_cz_suffix_keeps_exact_priority_and_ambiguity_rules():
    subtitle = Path("Series - 01.cz.ass")
    exact = Path("Series - 01.cz.mkv")
    assert safe_subtitle_matches([Path("Series - 01.mkv"), exact], subtitle) == (
        "exact", (exact,),
    )
    method, candidates = safe_subtitle_matches(
        [Path("Series - 01.mkv"), Path("Series - 01.mp4")], subtitle,
    )
    assert method == "language_suffix"
    assert len(candidates) == 2
