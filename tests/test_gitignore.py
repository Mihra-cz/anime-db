from pathlib import Path


def test_artwork_cache_is_ignored_by_git():
    assert "data/artwork/" in Path(".gitignore").read_text(encoding="utf-8").splitlines()
