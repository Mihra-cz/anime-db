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
