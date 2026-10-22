from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


_SCRATCH_READONLY = object()


class Base(DeclarativeBase):
    pass


def make_engine(database_url: str, *, maintenance=None):
    if maintenance is not None:
        maintenance.validate_engine_url(database_url)
    if (database_url.startswith("sqlite:///") and ":memory:" not in database_url
            and make_url(database_url).query.get("uri") != "true"):
        db_path = database_url.removeprefix("sqlite:///")
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        database_url,
        connect_args={"check_same_thread": False} if database_url.startswith("sqlite") else {},
    )
    if maintenance is not None:
        from threading import get_ident
        from .maintenance import MaintenanceStopped, _Lease, _BorrowedLease, _owner_scope

        engine.update_execution_options(scratch_maintenance=maintenance)
        admissions = {}

        def _require_process(*args):
            maintenance.check_process()

        def _check_lease(lease):
            try:
                if type(lease) is _Lease and not lease.exclusive and lease.boundary is maintenance:
                    maintenance._check_binding(lease)
                elif (type(lease) is _BorrowedLease and not lease._closed
                        and lease._pid == maintenance._pid
                        and _owner_scope.get() is lease.owner
                        and lease.owner._thread == get_ident()):
                    maintenance._check_binding(lease.owner)
                else:
                    raise MaintenanceStopped('STOP: missing or foreign writer lease')
            except MaintenanceStopped:
                raise
            except Exception as exc:
                raise MaintenanceStopped('STOP: writer lease is unconfirmed') from exc

        for lifecycle_event in ('connect', 'checkout', 'begin', 'commit', 'rollback'):
            event.listen(engine, lifecycle_event, _require_process)

        @event.listens_for(engine, "begin")
        def _admit_transaction(connection):
            info = connection.info
            dbapi = connection.connection.dbapi_connection
            option = connection.get_execution_options().get("scratch_readonly", False)
            if option is not False and option is not _SCRATCH_READONLY:
                raise MaintenanceStopped('STOP: unauthorized readonly option')
            readonly = option is _SCRATCH_READONLY
            admitted = admissions.get(id(info))
            lease = admitted[2] if admitted is not None else None
            if (admitted is not None and (admitted[0] is not info or admitted[1] is not dbapi)
                    or info.get("maintenance_lease") is not lease):
                raise MaintenanceStopped('STOP: stale connection admission')
            if lease is not None:
                _check_lease(lease)
            elif not readonly:
                lease = maintenance._acquire()
                info["maintenance_lease"] = lease
            # Independent evidence binds admission to this checked-out DBAPI
            # connection. connection.info alone is not writer authority.
            admissions[id(info)] = (info, dbapi, lease, readonly)
            dbapi.execute("PRAGMA query_only=" + ("ON" if readonly else "OFF"))

        @event.listens_for(engine, "before_cursor_execute")
        def _require_admission(connection, cursor, statement, parameters, context, many):
            maintenance.check_process()
            info = connection.info
            dbapi = connection.connection.dbapi_connection
            admitted = admissions.get(id(info))
            # A rejected SQLAlchemy autobegin may suppress later begin events.
            # Never allow that connection to execute in SQLite autocommit.
            if (admitted is None or admitted[0] is not info or admitted[1] is not dbapi
                    or not dbapi.in_transaction):
                raise MaintenanceStopped('STOP: SQL has no active connection admission')
            if admitted[3]:
                if (connection.get_execution_options().get("scratch_readonly") is not _SCRATCH_READONLY
                        or dbapi.execute("PRAGMA query_only").fetchone()[0] != 1):
                    raise MaintenanceStopped('STOP: readonly admission is unconfirmed')
            else:
                lease = admitted[2]
                if lease is None or info.get("maintenance_lease") is not lease:
                    raise MaintenanceStopped('STOP: SQL has no matching writer lease')
                _check_lease(lease)

        @event.listens_for(engine, "checkin")
        def _release_transaction(dbapi, record):
            # Pool checkin follows actual DBAPI commit/rollback (or disposal).
            # A Core connection held across commits conservatively retains admission.
            maintenance.check_process()
            admitted = admissions.pop(id(record.info), None)
            lease = admitted[2] if admitted is not None else None
            record.info.pop("maintenance_lease", None)
            try:
                if dbapi is not None:
                    dbapi.execute("PRAGMA query_only=OFF")
            finally:
                if lease is not None:
                    lease.close()
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def _configure_sqlite_connection(dbapi_connection, _connection_record):
            # pysqlite emits BEGIN implicitly only before DML and never before
            # SAVEPOINT.  A SAVEPOINT opened while the driver still believes no
            # transaction is running becomes the outermost transaction, and its
            # RELEASE therefore commits to disk; a later session rollback then
            # has nothing left to undo and a failed workflow stays half applied.
            # Handing transaction control to SQLAlchemy keeps every SAVEPOINT
            # nested inside the transaction the calling workflow owns.
            dbapi_connection.isolation_level = None
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
            finally:
                cursor.close()

        @event.listens_for(engine, "begin")
        def _begin_sqlite_transaction(connection):
            # Emitted straight on the DBAPI connection: this is transaction
            # control, not a query, and it must not appear to the statement
            # instrumentation that guards against N+1 loading.  Schema
            # migrations drive the same connection through raw DBAPI cursors,
            # so adopt a transaction that is already running instead of
            # starting a second one; SQLAlchemy ends either the same way.
            dbapi_connection = connection.connection.dbapi_connection
            if not dbapi_connection.in_transaction:
                dbapi_connection.execute("BEGIN")
    return engine


def make_session_factory(engine):
    maintenance = engine.get_execution_options().get('scratch_maintenance')
    if maintenance is not None:
        from .maintenance import _readonly_scope

        class ScratchSession(Session):
            def __init__(self, *args, readonly=None, **kwargs):
                if readonly is True or _readonly_scope.get():
                    kwargs["bind"] = engine.execution_options(scratch_readonly=_SCRATCH_READONLY)
                    kwargs["autoflush"] = False
                super().__init__(*args, **kwargs)

            def commit(self):
                maintenance.check_process()
                return super().commit()

            def rollback(self):
                maintenance.check_process()
                return super().rollback()

            def close(self):
                maintenance.check_process()
                return super().close()

        return sessionmaker(engine, class_=ScratchSession, expire_on_commit=False)
    return sessionmaker(engine, expire_on_commit=False)
