from pathlib import Path

from app.subtitles import detect_language, subtitle_matches


def test_subtitle_pairing():
    video = Path("Episode 01.mkv")
    assert subtitle_matches(video, Path("Episode 01.srt"))
    assert subtitle_matches(video, Path("Episode 01.cs.ass"))
    assert not subtitle_matches(video, Path("Episode 02.srt"))


def test_detects_czech_and_slovak():
    assert detect_language("Ahoj, jsem tady. Když přijdeš, řeknu ti něco, protože můžu.") == "cs"
    assert detect_language("Ahoj, som tu. Keď prídeš, poviem ti niečo, pretože môžem.") == "sk"
