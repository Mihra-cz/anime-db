"""One clean AnimeDB installation. No adoption, restore or automatic retry."""

from contextlib import closing, contextmanager
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
from uuid import UUID, uuid4


class FirstRunError(Exception):
    pass


def _directories(data, state):
    for path in (data, state):
        if (not path.is_dir() or path.resolve() != path.absolute()
                or not os.access(path, os.R_OK | os.W_OK | os.X_OK)):
            raise FirstRunError("DB/state directories must exist, without symlinks, and be writable")
    if os.path.samefile(data, state):
        raise FirstRunError("DB and installation state require separate directories")


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _record(path, value):
    # Never replace evidence. A partial record after a crash must block startup.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    _sync_directory(path.parent)


def _read_record(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd) as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 4096:
                raise FirstRunError("Invalid installation evidence file")
            value = json.load(stream)
        if (not isinstance(value, dict) or set(value) != {"format", "project", "installation_id", "schema"}
                or value["format"] != 1 or value["project"] != "anime-db" or value["schema"] != 9):
            raise ValueError("Unsupported registration")
        UUID(value["installation_id"])
        return value
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise FirstRunError("Installation evidence missing or invalid; no automatic create") from exc


def provision(data=Path("/data"), state=Path("/state")):
    """Explicit installer step: register a new empty slot without creating SQLite."""
    _directories(data, state)
    if any(data.iterdir()) or any(state.iterdir()):
        raise FirstRunError("Provision requires empty DB/state directories; never repeat after data loss")
    _record(state / "provisioned.json", {
        "format": 1, "project": "anime-db", "installation_id": str(uuid4()), "schema": 9,
    })


def _validate_database(database):
    from app.database import Base
    from app import models  # Register the existing model, without application startup.

    try:
        info = database.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise FirstRunError("DB must be a regular independent file")
        if any(database.parent.glob(database.name + "-*")):
            raise FirstRunError("SQLite sidecars require offline diagnosis; no automatic recovery")
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
            if connection.execute("PRAGMA user_version").fetchone()[0] != 9:
                raise FirstRunError("First-run installation requires schema v9")
            if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise FirstRunError("DB integrity check failed")
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise FirstRunError("DB foreign key check failed")
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )}
            if tables != set(Base.metadata.tables):
                raise FirstRunError("DB schema tables differ; no automatic repair")
    except (OSError, sqlite3.Error) as exc:
        raise FirstRunError("DB missing, lost or invalid; no automatic create") from exc


@contextmanager
def runtime_database(data=Path("/data"), state=Path("/state")):
    """Hold one runtime lock, consume first-install evidence before any DB write."""
    from app.database import make_engine
    from app.migrations import STARTUP_COMPATIBILITY_VERSION, migrate_schema_at_startup

    _directories(data, state)
    registration = _read_record(state / "provisioned.json")
    if STARTUP_COMPATIBILITY_VERSION != 9:
        raise FirstRunError("Review first-run support before a schema upgrade")
    lock = os.open(state / "runtime.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise FirstRunError("Another instance is running (runtime lock)") from exc
        database = data / "anime.db"
        # mode=rw also protects connections opened after this startup gate.
        url = "sqlite:///" + database.as_uri() + "?mode=rw&uri=true"
        if os.path.lexists(state / "ready.json"):
            if (_read_record(state / "ready.json") != registration
                    or _read_record(state / "initializing.json") != registration):
                raise FirstRunError("Installation evidence mismatch")
        else:
            if os.path.lexists(state / "initializing.json"):
                raise FirstRunError("Initialization interrupted; preserve files for diagnosis")
            if any(data.iterdir()):
                raise FirstRunError("Unexpected DB/files before initialization; no adoption")
            _record(state / "initializing.json", registration)
            fd = os.open(database, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.close(fd)
            engine = make_engine(url)
            try:
                migrate_schema_at_startup(engine)
            finally:
                engine.dispose()
            _validate_database(database)
            with database.open("rb") as stream:
                os.fsync(stream.fileno())
            _sync_directory(data)
            _record(state / "ready.json", registration)
        _validate_database(database)
        # The single Uvicorn process inherits the lock across exec, until exit.
        os.set_inheritable(lock, True)
        yield url
    finally:
        os.close(lock)


def main():
    try:
        if sys.argv[1:] == ["provision"]:
            provision()
            print("PROVISIONED anime-db: first app start will create schema v9")
            return 0
        import nexus_preflight as guard

        expected = {
            "DATABASE_URL": "sqlite:///file:///data/anime.db?mode=rw&uri=true",
            "ANIME_PATH": "/media/anime",
            "REQUIRE_MOUNT": "true", "METADATA_ARTWORK_DIRECTORY": "/artwork",
            **{name: "false" for name in (
                "METADATA_ENABLED", "ANILIST_ENABLED", "METADATA_DOWNLOAD_ARTWORK",
                "METADATA_ALLOW_REMOTE_IMAGES", "METADATA_AUTO_CONFIRM",
            )},
        }
        for name, value in expected.items():
            if os.environ.get(name) != value:
                raise FirstRunError(f"Unexpected {name}; use the Nexus compose.yaml")
        if os.geteuid() == 0 or os.getegid() == 0:
            raise FirstRunError("Non-root UID/GID required")
        if len(sys.argv) < 2:
            raise FirstRunError("Missing application command")
        mountinfo = Path("/proc/self/mountinfo").read_text()
        try:
            guard.require_readonly_cifs(mountinfo, "/media/anime")
            # Bind existence alone does not prove NVMe storage.
            records = guard._mount_records(mountinfo)
        except guard.PreflightError as exc:
            raise FirstRunError(str(exc)) from exc
        for target in ("/data", "/state", "/artwork"):
            mounts = [r for r in records if str(r["point"]) == target]
            if (len(mounts) != 1 or "rw" not in mounts[0]["options"]
                    or not mounts[0]["source"].startswith("/dev/nvme")
                    or mounts[0]["filesystem"] not in {"ext4", "xfs", "btrfs"}):
                raise FirstRunError(f"{target} requires a writable local NVMe bind")
        artwork = Path("/artwork")
        if not artwork.is_dir() or not os.access(artwork, os.R_OK | os.W_OK | os.X_OK):
            raise FirstRunError("Artwork directory missing or not writable")
        with runtime_database() as url:
            try:
                guard.require_prepared_database(Path("/data/anime.db"))
            except guard.PreflightError as exc:
                raise FirstRunError(str(exc)) from exc
            os.environ["DATABASE_URL"] = url
            os.execvp(sys.argv[1], sys.argv[1:])
    except (FirstRunError, OSError) as exc:
        print(f"STOP: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
