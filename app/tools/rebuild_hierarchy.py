from __future__ import annotations

import argparse

from app.config import get_settings
from app.database import make_engine
from app.hierarchy_rebuild import ReconciliationAction, rebuild_hierarchy
from sqlalchemy.orm import Session


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sestaví nebo aplikuje globální reconciliation logické hierarchie."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    engine = make_engine(get_settings().database_url)
    with Session(engine) as session:
        result = rebuild_hierarchy(session, apply=args.apply)
    plan = result.plan
    print("REŽIM:", "APPLY" if args.apply else "DRY-RUN")
    print("FINGERPRINT:", plan.source_fingerprint)
    for item in plan.collections:
        if item.action == ReconciliationAction.PRESERVE:
            continue
        print(
            f"collection action={item.action.value} path={item.relative_root_path!r} "
            f"reason={item.reason.value}"
        )
    for item in plan.titles:
        if item.action == ReconciliationAction.PRESERVE:
            continue
        print(
            f"title action={item.action.value} id={item.title_id or 'new'} "
            f"path={item.relative_root_path!r} protected={item.protected} "
            f"reason={item.reason.value}"
        )
    for item in plan.video_assignments:
        if not item.changed:
            continue
        print(
            f"video id={item.video_id} path={item.relative_path!r} "
            f"collection={item.old_collection_path!r}->{item.target_collection_path!r} "
            f"title={item.old_title_path!r}->{item.target_title_path!r} "
            f"decision={item.manual_split_kind or 'none'} reason={item.reason.value}"
        )
    for blocker in plan.blockers:
        print(
            f"blocker code={blocker.code} collection={blocker.collection_path!r} "
            f"title={blocker.title_path!r} prevents_apply={blocker.prevents_apply}"
        )
    summary = plan.summary
    print(
        "SOUHRN: "
        f"collections +{summary.collections_created}/~{summary.collections_updated}/"
        f"-{summary.collections_removed}; "
        f"titles +{summary.titles_created}/~{summary.titles_updated}/"
        f"-{summary.titles_removed}; "
        f"assignments={summary.video_assignments_changed}; "
        f"numbering={summary.numbering_changes}; "
        f"issues={summary.blocking_issues}; conflicts={summary.conflicts}; "
        f"logical_changes={summary.logical_changes}"
    )


if __name__ == "__main__":
    main()
