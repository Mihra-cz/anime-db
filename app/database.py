from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker


class Base(DeclarativeBase):
    pass


def make_engine(database_url: str):
    if database_url.startswith("sqlite:///") and ":memory:" not in database_url:
        db_path = database_url.removeprefix("sqlite:///")
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        database_url,
        connect_args={"check_same_thread": False} if database_url.startswith("sqlite") else {},
    )
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
    return sessionmaker(engine, expire_on_commit=False)
