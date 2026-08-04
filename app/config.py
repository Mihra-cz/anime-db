from dataclasses import dataclass
import os
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    anime_path: Path
    database_url: str


def get_settings(dotenv_path: str | Path | None = None) -> Settings:
    # override=False zachovává prioritu proměnných zděděných ze systému.
    load_dotenv(dotenv_path=dotenv_path, override=False)
    return Settings(
        anime_path=Path(os.getenv("ANIME_PATH", "/media/anime")),
        database_url=os.getenv("DATABASE_URL", "sqlite:///./data/anime.db"),
    )
