"""Check Nexus mounts and a prepared working DB before importing AnimeDB.

This guard never creates directories, opens SQLite or initializes a database.
It is used only by the Nexus Compose entrypoint; local Uvicorn is unchanged.
"""

import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys


class PreflightError(Exception):
    pass


_LIBRARY_SOURCE = "//192.168.11.149/Anime"
_LIBRARY_ADDRESS = "192.168.11.149"
_NETWORK_FILESYSTEMS = frozenset({
    "cifs", "smb3", "nfs", "nfs4", "smbfs", "9p", "virtiofs",
    "ceph", "cephfs", "glusterfs", "afs",
})


def _mount_field(value: str) -> str:
    escapes = {"040": " ", "011": "\t", "012": "\n", "134": "\\"}
    if re.search(r"\\(?!040|011|012|134)", value):
        raise ValueError("Unknown mountinfo escape")
    return re.sub(r"\\(040|011|012|134)", lambda match: escapes[match[1]], value)


def _mount_records(raw: str) -> list[dict]:
    records, ids = [], set()
    try:
        for line in raw.splitlines():
            if not line.strip():
                continue
            before, separator, after = line.partition(" - ")
            fields, filesystem = before.split(), after.split()
            if not separator or len(fields) < 6 or len(filesystem) != 3:
                raise ValueError("Incomplete mountinfo record")
            mount_id, parent_id = int(fields[0]), int(fields[1])
            if mount_id <= 0 or parent_id < 0 or mount_id in ids or not re.fullmatch(r"\d+:\d+", fields[2]):
                raise ValueError("Invalid mount identity")
            ids.add(mount_id)
            root, point = (_mount_field(value) for value in fields[3:5])
            for path in (root, point):
                if (not path.startswith("/") or path.startswith("//")
                        or str(PurePosixPath(path)) != path or ".." in PurePosixPath(path).parts):
                    raise ValueError("Noncanonical mount path")
            options, super_options = set(fields[5].split(",")), set(filesystem[2].split(","))
            if any(len(options_set & {"ro", "rw"}) != 1 for options_set in (options, super_options)):
                raise ValueError("Ambiguous mount permissions")
            records.append({"id": mount_id, "parent": parent_id,
                            "point": PurePosixPath(point), "root": root,
                            "device": tuple(map(int, fields[2].split(":"))),
                            "filesystem": filesystem[0], "source": _mount_field(filesystem[1]),
                            "options": options, "super_options": super_options})
    except ValueError as exc:
        raise PreflightError("Cannot verify library boundary from mountinfo.") from exc
    return records


def require_readonly_cifs(mountinfo: str, target: str) -> None:
    records = _mount_records(mountinfo)
    mounts = [record for record in records if str(record["point"]) == target]
    if len(mounts) != 1:
        raise PreflightError("Library must have one unambiguous CIFS mount at /media/anime.")
    library = mounts[0]
    if library["filesystem"] != "cifs" or "ro" not in library["options"]:
        raise PreflightError("Library requires an active CIFS mount with container read-only VFS.")
    if (library["source"] != _LIBRARY_SOURCE or library["root"] != "/"
            or any((option.startswith("addr=") and option != "addr=" + _LIBRARY_ADDRESS)
                   or (option.startswith("prefixpath=") and option not in ("prefixpath=", "prefixpath=/"))
                   for option in library["super_options"])):
        raise PreflightError("Library CIFS source identity differs from the approved NAS share.")
    # A CIFS record can remain in mountinfo after an ancestor is overmounted.
    # Its mount-parent chain must agree with the mounts above its visible path.
    by_id = {record["id"]: record for record in records}
    ancestors, current = set(), library
    while current is not None:
        if current["id"] in ancestors:
            raise PreflightError("Cannot verify library mount ancestry.")
        ancestors.add(current["id"])
        parent = by_id.get(current["parent"])
        if parent is current and current["point"] == PurePosixPath("/"):
            break
        if parent is not None and parent["point"] != current["point"] and parent["point"] not in current["point"].parents:
            raise PreflightError("Cannot verify library mount ancestry.")
        current = parent
    # Superblock rw is compatible with a read-only bind. Check this namespace's
    # other mounts too: another bind or separately mounted share can bypass it.
    for record in records:
        if record is library:
            continue
        if record["point"] in library["point"].parents and record["id"] not in ancestors:
            raise PreflightError("Library CIFS mount is hidden by an ancestor mount.")
        if library["point"] in record["point"].parents:
            raise PreflightError("Additional mounts inside the library are not allowed.")
        if "rw" in record["options"] and (record["device"] == library["device"]
                or record["filesystem"] in _NETWORK_FILESYSTEMS
                or record["filesystem"].startswith("fuse.")):
            raise PreflightError("Web runtime must not expose a writable NAS mount or library alias.")


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
