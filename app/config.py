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
    metadata_allow_remote_images: bool = True
    metadata_candidate_limit: int = 10
    metadata_batch_search_limit: int = 10
    metadata_artwork_max_bytes: int = 10_485_760
    metadata_artwork_thumbnail_width: int = 400
    metadata_artwork_directory: Path = Path("data/artwork")
    preferred_title_language: str = "romaji"
    ffprobe_timeout_seconds: float = 60
    mediainfo_timeout_seconds: float = 60
    library_access_timeout_seconds: float = 10
    library_healthcheck_interval_files: int = 25


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
        metadata_allow_remote_images=_get_bool("METADATA_ALLOW_REMOTE_IMAGES", True),
        metadata_candidate_limit=max(1, min(10, int(os.getenv("METADATA_CANDIDATE_LIMIT", "10")))),
        metadata_batch_search_limit=max(1, int(os.getenv("METADATA_BATCH_SEARCH_LIMIT", "10"))),
        metadata_artwork_max_bytes=max(1, int(os.getenv("METADATA_ARTWORK_MAX_BYTES", "10485760"))),
        metadata_artwork_thumbnail_width=max(1, int(os.getenv("METADATA_ARTWORK_THUMBNAIL_WIDTH", "400"))),
        metadata_artwork_directory=Path(os.getenv("METADATA_ARTWORK_DIRECTORY", "data/artwork")),
        preferred_title_language=os.getenv(
            "PREFERRED_TITLE_LANGUAGE", "romaji"
        ).strip().casefold(),
        ffprobe_timeout_seconds=max(0.1, float(os.getenv("FFPROBE_TIMEOUT_SECONDS", "60"))),
        mediainfo_timeout_seconds=max(0.1, float(os.getenv("MEDIAINFO_TIMEOUT_SECONDS", "60"))),
        library_access_timeout_seconds=max(0.1, float(os.getenv("LIBRARY_ACCESS_TIMEOUT_SECONDS", "10"))),
        library_healthcheck_interval_files=max(1, int(os.getenv("LIBRARY_HEALTHCHECK_INTERVAL_FILES", "25"))),
    )
