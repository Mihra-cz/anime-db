"""Separate E6B cooperating process; all paths are supplied private scratch data."""
from contextlib import ExitStack
from pathlib import Path
import sys

from sqlalchemy import text

from app.database import make_engine, make_session_factory
from app.maintenance import ScratchMaintenance, MaintenanceBusy


def main():
    mode, database, root = sys.argv[1:]
    gate = ScratchMaintenance(Path(database), Path(root))
    engine = None
    try:
        with ExitStack() as stack:
            if mode in {'exclusive', 'owner_db'}:
                owner = stack.enter_context(gate.maintenance())
            elif mode == 'shared':
                stack.enter_context(gate.writer())
            if mode in {'transaction', 'owner_db'}:
                engine = make_engine(f'sqlite:///{database}', maintenance=gate)
                stack.callback(engine.dispose)
                if mode == 'owner_db':
                    with owner.write_scope(), engine.begin() as connection:
                        connection.execute(text("UPDATE sample SET value='owner committed'"))
                    (Path(root) / 'unfinished-operation').write_text('unverified physical state')
                else:
                    session = stack.enter_context(make_session_factory(engine)())
                    session.execute(text("UPDATE sample SET value='writer committed'"))
            print('READY', flush=True)
            command = sys.stdin.readline().strip()
            if mode == 'transaction':
                if command == 'COMMIT':
                    session.commit()
                else:
                    session.rollback()
        print('DONE', flush=True)
    except MaintenanceBusy:
        print('BUSY', flush=True)
        return 3
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
