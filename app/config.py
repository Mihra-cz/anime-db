from dataclasses import dataclass
import os
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    anime_path: Path
    database_url: str
    require_mount: bool = False


def _get_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def get_settings(dotenv_path: str | Path | None = None) -> Settings:
    # override=False zachovává prioritu proměnných zděděných ze systému.
    load_dotenv(dotenv_path=dotenv_path, override=False)
    return Settings(
        anime_path=Path(os.getenv("ANIME_PATH", "/media/anime")),
        database_url=os.getenv("DATABASE_URL", "sqlite:///./data/anime.db"),
        require_mount=_get_bool("REQUIRE_MOUNT"),
    )
