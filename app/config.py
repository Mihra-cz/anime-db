from dataclasses import dataclass
import os
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    anime_path: Path
    database_url: str
    require_mount: bool = False
    metadata_enabled: bool = True
    metadata_primary_provider: str = "anilist"
    metadata_request_timeout_seconds: float = 15
    metadata_cache_ttl_hours: int = 168
    metadata_auto_confirm: bool = False
    metadata_auto_confirm_threshold: float = 0.95
    metadata_download_artwork: bool = True
    anilist_enabled: bool = True


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
        metadata_enabled=_get_bool("METADATA_ENABLED", True),
        metadata_primary_provider=os.getenv("METADATA_PRIMARY_PROVIDER", "anilist").strip().casefold(),
        metadata_request_timeout_seconds=float(os.getenv("METADATA_REQUEST_TIMEOUT_SECONDS", "15")),
        metadata_cache_ttl_hours=int(os.getenv("METADATA_CACHE_TTL_HOURS", "168")),
        metadata_auto_confirm=_get_bool("METADATA_AUTO_CONFIRM", False),
        metadata_auto_confirm_threshold=float(os.getenv("METADATA_AUTO_CONFIRM_THRESHOLD", "0.95")),
        metadata_download_artwork=_get_bool("METADATA_DOWNLOAD_ARTWORK", True),
        anilist_enabled=_get_bool("ANILIST_ENABLED", True),
    )
