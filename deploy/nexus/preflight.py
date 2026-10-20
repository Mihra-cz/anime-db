"""Check Nexus mounts and a prepared working DB before importing AnimeDB.

This guard never creates directories, opens SQLite or initializes a database.
It is used only by the Nexus Compose entrypoint; local Uvicorn is unchanged.
"""

import os
from pathlib import Path
import stat
import sys


class PreflightError(Exception):
    pass


def require_readonly_cifs(mountinfo: str, target: str) -> None:
    mounts = []
    for line in mountinfo.splitlines():
        before, separator, after = line.partition(" - ")
        fields, filesystem = before.split(), after.split()
        if separator and len(fields) >= 6 and len(filesystem) >= 3:
            if fields[4] == target:
                mounts.append((fields[5].split(","), filesystem[0], filesystem[2].split(",")))
    if len(mounts) != 1:
        raise PreflightError("Library must have one unambiguous CIFS mount at /media/anime.")
    options, filesystem, super_options = mounts[0]
    if filesystem != "cifs" or "ro" not in options or "ro" not in super_options:
        raise PreflightError("Library requires an active CIFS mount with host and container read-only.")


def require_prepared_database(database: Path) -> None:
    try:
        descriptor = os.open(database, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            details = os.fstat(source.fileno())
            if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
                raise PreflightError("Working DB must be a regular independent file, without links.")
            header = source.read(100)
    except OSError as exc:
        raise PreflightError("Prepared working DB is missing or unreadable; initialization is a separate step.") from exc
    if (len(header) != 100 or header[:16] != b"SQLite format 3\x00"
            or int.from_bytes(header[60:64], "big") == 0):
        raise PreflightError("Working DB must already have a versioned SQLite schema; initialization is a separate step.")
    if not (os.access(database, os.R_OK | os.W_OK)
            and os.access(database.parent, os.R_OK | os.W_OK | os.X_OK)):
        raise PreflightError("Working DB and its directory must be writable by the runtime UID/GID.")


def main() -> int:
    try:
        expected = {
            "DATABASE_URL": "sqlite:////data/anime.db",
            "ANIME_PATH": "/media/anime",
            "REQUIRE_MOUNT": "true",
            "METADATA_ARTWORK_DIRECTORY": "/artwork",
        }
        for name, value in expected.items():
            if os.environ.get(name) != value:
                raise PreflightError(f"Unexpected {name}; use the Nexus Compose configuration.")
        if os.geteuid() == 0 or os.getegid() == 0:
            raise PreflightError("Nexus runtime requires a non-root UID and GID.")
        require_readonly_cifs(Path("/proc/self/mountinfo").read_text(), expected["ANIME_PATH"])
        require_prepared_database(Path("/data/anime.db"))
        artwork = Path(expected["METADATA_ARTWORK_DIRECTORY"])
        if not artwork.is_dir() or not os.access(artwork, os.R_OK | os.W_OK | os.X_OK):
            raise PreflightError("Artwork bind directory must exist and be writable by the runtime UID/GID.")
        if len(sys.argv) < 2:
            raise PreflightError("Missing application command.")
        os.execvp(sys.argv[1], sys.argv[1:])
    except (PreflightError, OSError) as exc:
        print(f"STOP: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
