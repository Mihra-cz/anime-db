"""Print a read-only V6 target plan as JSON; no execution or saved manifest.

Exit status: 0 clean plan, 2 plan with BLOCKED/REVIEW records, 1 failed
determinism or DB fingerprint evidence (or an unexpected error).

Example:
    python -m app.tools.target_plan --db data/anime.db --library-root /mnt/nas-anime --repeat 2
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sqlite3
from urllib.parse import quote

from sqlalchemy import event

from ..target_planner_filesystem import inventory_filesystem, plan_library
from ..target_planner_service import load_planner_context, readonly_planner_session
from ..target_planner_post_state import simulate_post_state


def database_fingerprint(path: Path) -> dict:
    info = path.stat()
    uri = 'file:' + quote(str(path.absolute()), safe='/') + '?mode=ro'
    with sqlite3.connect(uri, uri=True) as connection:
        version = connection.execute('SELECT user_version FROM pragma_user_version').fetchone()[0]
    with path.open('rb') as source:
        digest = hashlib.file_digest(source, 'sha256').hexdigest()
    return dict(user_version=version, size=info.st_size, mtime_ns=info.st_mtime_ns, sha256=digest)


def simulate_readonly(database_path: Path, library_root: Path, *, repeat: int = 2, windows_root: str | None = None, post_state: bool = False) -> dict:
    """Fresh DB preload and one inventory traversal per run; report evidence."""
    if type(repeat) is not int or repeat < 1:
        raise ValueError('repeat must be a positive integer')
    database_path = Path(database_path)
    before = database_fingerprint(database_path)
    plans = []
    select_counts = []
    session_states = []
    projections = []
    for _ in range(repeat):
        with readonly_planner_session(database_path) as session:
            statements = []
            def select_only(_conn, _cursor, statement, _parameters, _context, _executemany):
                if not statement.lstrip().upper().startswith('SELECT'):
                    raise RuntimeError('Planner attempted a non-SELECT statement')
                statements.append(statement)
            event.listen(session.get_bind(), 'before_cursor_execute', select_only)
            context = load_planner_context(session)
            select_counts.append(len(statements))
            session_states.append([len(session.new), len(session.dirty), len(session.deleted)])
        snapshot = inventory_filesystem(Path(library_root))
        plan = plan_library(context, snapshot, windows_root=windows_root)
        plans.append(plan)
        if post_state:
            try:
                projected = simulate_post_state(context, snapshot, plan, windows_root=windows_root)
                projections.append(dict(plan=asdict(projected.plan),
                    locators=[asdict(r) for r in projected.locators],
                    execution_evidence=[asdict(r) for r in projected.execution_evidence],
                    converged=projected.converged, blockers=list(projected.blockers), projection_hash=projected.projection_hash))
            except ValueError as error:
                projections.append(dict(converged=False, blockers=[str(error)], projection_hash=None))
    after = database_fingerprint(database_path)
    result = dict(plan=asdict(plans[0]), deterministic=all(plan == plans[0] for plan in plans),
        plan_hashes=[p.plan_hash for p in plans], filesystem_hashes=[p.filesystem_hash for p in plans],
        sql_select_counts=select_counts, session_states=session_states,
        production_db_before=before, production_db_after=after, production_db_unchanged=before == after,
        nas_write_operations=0)
    if post_state:
        result.update(post_state=projections[0], post_state_deterministic=all(p == projections[0] for p in projections),
            post_state_hashes=[p['projection_hash'] for p in projections])
    return result


def exit_status(result: dict) -> int:
    if not (result['deterministic'] and result['production_db_unchanged']):
        return 1
    if 'post_state' in result:
        if not result['post_state_deterministic']:
            return 1
        if not result['post_state']['converged']:
            return 2
    states = dict(result['plan']['status_counts'])
    return 2 if states.get('BLOCKED') or states.get('REVIEW') else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=Path('data/anime.db'))
    parser.add_argument('--library-root', type=Path, required=True)
    parser.add_argument('--windows-root', help='Actual absolute Windows client root; never guessed')
    parser.add_argument('--repeat', type=int, default=2)
    parser.add_argument('--post-state', action='store_true', help='Pure locator-only intended-state projection; no DB/FS writes or scanner')
    args = parser.parse_args(argv)
    result = simulate_readonly(args.db, args.library_root, repeat=args.repeat, windows_root=args.windows_root, post_state=args.post_state)
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return exit_status(result)


if __name__ == '__main__':
    raise SystemExit(main())
