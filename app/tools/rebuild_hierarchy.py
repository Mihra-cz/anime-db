from __future__ import annotations

import argparse

from app.config import get_settings
from app.database import make_engine
from app.hierarchy_rebuild import rebuild_hierarchy
from sqlalchemy.orm import Session


def main() -> None:
    parser = argparse.ArgumentParser(description="Znovu vyhodnotí bezpečně rozpoznatelné části.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    engine = make_engine(get_settings().database_url)
    with Session(engine) as session:
        changes = rebuild_hierarchy(session, apply=args.apply)
    print("REŽIM:", "APPLY" if args.apply else "DRY-RUN")
    for change in changes:
        print(
            f"title={change.title_id} folder={change.original_folder_name!r} "
            f"base={change.normalized_base!r} season={change.old_season_label or '—'}"
            f"->{change.new_season_label or '—'} collection={change.collection_path!r} "
            f"reason={change.reason}"
        )
    print(f"Navrženo změn: {len(changes)}")


if __name__ == "__main__":
    main()
