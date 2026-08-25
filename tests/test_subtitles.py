from pathlib import Path

from app.subtitles import detect_language, safe_subtitle_matches, subtitle_matches


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
