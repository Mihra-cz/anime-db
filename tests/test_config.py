from pathlib import Path

from app.config import get_settings


def test_loads_dotenv_but_system_environment_has_priority(tmp_path: Path, monkeypatch):
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        "ANIME_PATH=/from-dotenv\nDATABASE_URL=sqlite:////from-dotenv.db\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("ANIME_PATH", raising=False)
    monkeypatch.delenv("REQUIRE_MOUNT", raising=False)
    monkeypatch.setenv("DATABASE_URL", "sqlite:////from-system.db")

    settings = get_settings(dotenv_path)

    assert settings.anime_path == Path("/from-dotenv")
    assert settings.database_url == "sqlite:////from-system.db"
    assert settings.require_mount is False


def test_remote_images_can_be_disabled(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("METADATA_ALLOW_REMOTE_IMAGES", "false")
    assert get_settings(tmp_path / "missing.env").metadata_allow_remote_images is False


def test_v5_candidate_and_artwork_defaults(tmp_path: Path, monkeypatch):
    for name in ("METADATA_CANDIDATE_LIMIT", "METADATA_BATCH_SEARCH_LIMIT", "METADATA_ARTWORK_MAX_BYTES", "METADATA_ARTWORK_THUMBNAIL_WIDTH"):
        monkeypatch.delenv(name, raising=False)
    settings = get_settings(tmp_path / "missing.env")
    assert settings.metadata_candidate_limit == 10
    assert settings.metadata_batch_search_limit == 10
    assert settings.metadata_artwork_max_bytes == 10_485_760
    assert settings.metadata_artwork_thumbnail_width == 400


def test_probe_and_library_timeout_defaults(tmp_path: Path, monkeypatch):
    for name in ("FFPROBE_TIMEOUT_SECONDS", "MEDIAINFO_TIMEOUT_SECONDS", "LIBRARY_ACCESS_TIMEOUT_SECONDS", "LIBRARY_HEALTHCHECK_INTERVAL_FILES"):
        monkeypatch.delenv(name, raising=False)
    settings = get_settings(tmp_path / "missing.env")
    assert settings.ffprobe_timeout_seconds == 60
    assert settings.mediainfo_timeout_seconds == 60
    assert settings.library_access_timeout_seconds == 10
    assert settings.library_healthcheck_interval_files == 25


def test_default_preferred_title_language_is_romaji(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("PREFERRED_TITLE_LANGUAGE", raising=False)
    assert get_settings(tmp_path / "missing.env").preferred_title_language == "romaji"
