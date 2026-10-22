"""Deployment guards only: no application import, SQLite connection or NAS access."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest


pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Nexus guard uses Linux procfs and file flags")


SCRIPT = Path(__file__).resolve().parents[1] / "deploy/nexus/preflight.py"


def load_preflight():
    assert SCRIPT.is_file(), "Nexus preflight must run before importing the application"
    spec = importlib.util.spec_from_file_location("nexus_preflight", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("super_options", ["ro,vers=3.0", "rw,vers=3.0"])
def test_accepts_readonly_cifs_bind(super_options):
    preflight = load_preflight()
    preflight.require_readonly_cifs(
        f"40 20 0:44 / /media/anime ro,relatime - cifs //192.168.11.149/Anime {super_options}\n",
        "/media/anime",
    )


@pytest.mark.parametrize("mountinfo", [
    "20 1 259:2 / / ro,relatime - ext4 /dev/nvme0n1p2 ro\n",
    "40 20 259:2 /mnt/nas-anime /media/anime ro - ext4 /dev/nvme0n1p2 ro\n",
    "40 20 0:44 / /media/anime ro - autofs systemd-1 ro\n",
    "40 20 0:44 / /media/anime rw - cifs //192.168.11.149/Anime ro\n",
    "40 20 0:44 / /media/anime rw - cifs //192.168.11.149/Anime rw\n",
    "40 20 0:44 / /media/anime ro,rw - cifs //192.168.11.149/Anime rw\n",
    "40 20 0:44 / /media/anime ro - cifs //192.168.11.149/Anime ro,rw\n",
    "40 20 0:44 / /media/anime-other ro - cifs //192.168.11.149/Anime ro\n",
    "40 20 0:44 / /media/anime ro - cifs //192.168.11.149/Anime ro\n"
    "41 20 259:2 / /media/anime ro - ext4 /dev/nvme0n1p2 ro\n",
    "malformed mountinfo\n",
])
def test_rejects_missing_automount_local_or_writable_library(mountinfo):
    preflight = load_preflight()
    with pytest.raises(preflight.PreflightError):
        preflight.require_readonly_cifs(mountinfo, "/media/anime")


@pytest.mark.parametrize("library", [
    "40 20 0:44 / /media/anime ro - cifs //192.168.11.150/Anime ro",
    "40 20 0:44 / /media/anime ro - cifs //192.168.11.149/Other ro",
    "40 20 0:44 / /media/anime ro - cifs //192.168.11.149/Anime/subdir ro",
    "40 20 0:44 /subdir /media/anime ro - cifs //192.168.11.149/Anime ro",
    "40 20 0:44 / /media/anime ro - cifs //192.168.11.149/Anime ro,addr=192.168.11.150",
    "40 20 0:44 / /media/anime ro - cifs //192.168.11.149/Anime ro,prefixpath=Season",
], ids=["wrong-server", "wrong-share", "source-subtree", "root-subtree", "wrong-address", "prefix-subtree"])
def test_rejects_unapproved_library_identity(library):
    preflight = load_preflight()
    with pytest.raises(preflight.PreflightError):
        preflight.require_readonly_cifs(library + "\n", "/media/anime")


@pytest.mark.parametrize("alias", [
    "41 20 0:44 / /writable rw - cifs //192.168.11.149/Anime rw",
    "41 20 0:44 /Season /writable rw - cifs //192.168.11.149/Anime rw",
    "41 20 0:45 / /writable rw - cifs //192.168.11.149/Anime rw",
    "41 20 0:44 / /writable rw - cifs //nas/Anime rw",
    "41 20 0:44 / /writable rw - ext4 /dev/alias rw",
    "41 20 0:45 / /backup rw - cifs //192.168.11.149/Nexus-Backups rw",
    "41 20 0:45 / /writable rw - smb3 //nas/Anime rw",
    "41 20 0:45 / /writable rw - nfs nas:/Anime rw",
    "41 20 0:44 / /writable\\040alias rw - cifs //nas/Anime rw",
], ids=["whole-share", "subtree", "other-superblock", "server-alias", "same-device",
        "backup-share", "smb3", "nfs", "escaped-target"])
def test_rejects_writable_network_mount_or_library_alias(alias):
    preflight = load_preflight()
    with pytest.raises(preflight.PreflightError):
        preflight.require_readonly_cifs(
            "40 20 0:44 / /media/anime ro - cifs //192.168.11.149/Anime ro\n" + alias + "\n",
            "/media/anime",
        )


@pytest.mark.parametrize("filesystem", [
    "smbfs", "9p", "virtiofs", "cephfs", "ceph", "glusterfs", "afs",
    "fuse.sshfs", "fuse.rclone", "fuse.glusterfs", "fuse.custom",
])
def test_rejects_additional_writable_network_filesystems(filesystem):
    preflight = load_preflight()
    with pytest.raises(preflight.PreflightError):
        preflight.require_readonly_cifs(
            "40 20 0:44 / /media/anime ro - cifs //192.168.11.149/Anime rw\n"
            f"41 20 0:45 / /remote rw - {filesystem} remote rw\n",
            "/media/anime",
        )


def test_allows_readonly_alias_and_local_writable_data_mounts():
    load_preflight().require_readonly_cifs(
        "20 1 0:30 / / ro - overlay overlay rw\n"
        "40 20 0:44 / /media/anime ro - cifs //192.168.11.149/Anime rw\n"
        "41 20 0:44 /Season /readonly\\040alias ro - cifs //192.168.11.149/Anime rw\n"
        "42 20 259:2 /db /data rw - ext4 /dev/nvme0n1p2 rw\n",
        "/media/anime",
    )


@pytest.mark.parametrize("extra", [
    "41 40 0:45 / /media/anime/nested rw - tmpfs tmpfs rw",
    "41 40 0:45 / /media/anime/nested ro - cifs //192.168.11.149/Other ro",
    "41 20 0:44 / /media/anime rw - cifs //192.168.11.149/Anime rw",
    "40 20 0:45 / /other ro - tmpfs tmpfs ro",
    "41 20 0:45 / /other ro - cifs",
    "malformed record hiding an alias",
    "41 20 0:45 / /other\\057alias rw - cifs //nas/Anime rw",
], ids=["nested-rw", "nested-other-share", "stacked-library", "duplicate-id",
        "truncated-record", "malformed-record", "unknown-escape"])
def test_stops_when_mount_table_cannot_prove_library_boundary(extra):
    preflight = load_preflight()
    with pytest.raises(preflight.PreflightError):
        preflight.require_readonly_cifs(
            "40 20 0:44 / /media/anime ro - cifs //192.168.11.149/Anime ro\n" + extra + "\n",
            "/media/anime",
        )


@pytest.mark.parametrize("overmount", [
    "41 20 0:45 / /media rw - tmpfs tmpfs rw",
    "41 20 0:45 / /media ro - tmpfs tmpfs rw",
    "41 20 0:45 / / ro - tmpfs tmpfs rw",
], ids=["rw-ancestor", "ro-ancestor", "stacked-root"])
def test_rejects_cifs_hidden_by_ancestor_overmount(overmount):
    preflight = load_preflight()
    with pytest.raises(preflight.PreflightError):
        preflight.require_readonly_cifs(
            "20 1 0:30 / / ro - overlay overlay rw\n"
            "40 20 0:44 / /media/anime ro - cifs //192.168.11.149/Anime rw\n"
            + overmount + "\n", "/media/anime",
        )


def test_allows_cifs_attached_to_visible_parent_mount():
    load_preflight().require_readonly_cifs(
        "20 1 0:30 / / ro - overlay overlay rw\n"
        "30 20 0:45 / /media ro - tmpfs tmpfs rw\n"
        "40 30 0:44 / /media/anime ro - cifs //192.168.11.149/Anime rw\n",
        "/media/anime",
    )


def test_rejects_parent_mount_with_unrelated_path():
    preflight = load_preflight()
    with pytest.raises(preflight.PreflightError):
        preflight.require_readonly_cifs(
            "20 1 0:30 / / ro - overlay overlay rw\n"
            "30 20 0:45 / /unrelated ro - tmpfs tmpfs rw\n"
            "40 30 0:44 / /media/anime ro - cifs //192.168.11.149/Anime rw\n",
            "/media/anime",
        )


@pytest.mark.parametrize("escape", [r"\057", r"\z"])
def test_rejects_unknown_escape_in_unrelated_readonly_mount(escape):
    preflight = load_preflight()
    with pytest.raises(preflight.PreflightError):
        preflight.require_readonly_cifs(
            "40 20 0:44 / /media/anime ro - cifs //192.168.11.149/Anime rw\n"
            f"41 20 0:45 / /readonly{escape}alias ro - tmpfs tmpfs rw\n",
            "/media/anime",
        )


@pytest.mark.parametrize("escape", [r"\040", r"\011", r"\012", r"\134"])
def test_accepts_legitimate_mountinfo_escapes(escape):
    load_preflight().require_readonly_cifs(
        "40 20 0:44 / /media/anime ro - cifs //192.168.11.149/Anime rw\n"
        f"41 20 0:45 / /readonly{escape}alias ro - tmpfs tmpfs rw\n",
        "/media/anime",
    )


def test_rejects_cyclic_library_mount_ancestry():
    preflight = load_preflight()
    with pytest.raises(preflight.PreflightError):
        preflight.require_readonly_cifs(
            "30 40 0:45 / /media ro - tmpfs tmpfs rw\n"
            "40 30 0:44 / /media/anime ro - cifs //192.168.11.149/Anime rw\n",
            "/media/anime",
        )


@pytest.fixture(scope="module")
def rendered_nexus_compose():
    if shutil.which("docker") is None:
        pytest.skip("Docker Compose CLI is required for daemon-free config validation")
    repo = SCRIPT.parents[2]
    result = subprocess.run(
        ["docker", "compose", "--env-file", str(repo / "deploy/nexus/.env.example"),
         "-f", str(repo / "deploy/nexus/compose.yaml"),
         "config", "--format", "json"],
        cwd=repo, capture_output=True, text=True, timeout=20,
        env={"PATH": os.environ["PATH"], "HOME": os.environ["HOME"]},
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_compose_keeps_web_library_readonly_and_without_extra_host_binds(rendered_nexus_compose):
    assert set(rendered_nexus_compose["services"]) == {"app"}
    app = rendered_nexus_compose["services"]["app"]
    mounts = app["volumes"]
    assert len(mounts) == 4
    library = next(m for m in mounts if m["target"] == "/media/anime")
    assert library["type"] == "bind"
    assert library["source"] == "/mnt/nas-anime"
    assert library["read_only"] is True
    assert library["bind"]["create_host_path"] is False
    assert library["bind"]["propagation"] == "rprivate"
    assert {(m["source"], m["target"]) for m in mounts if m is not library} == {
        ("/srv/nexus/appdata/anime-db/db", "/data"),
        ("/srv/nexus/appdata/anime-db/state", "/state"),
        ("/srv/nexus/appdata/anime-db/artwork", "/artwork"),
    }
    assert app["read_only"] is True
    assert app["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in app["security_opt"]
    assert app.get("privileged", False) is False


def sqlite_header(version=9):
    # A header fixture, not a database: no SQLite initialization is performed.
    header = bytearray(100)
    header[:16] = b"SQLite format 3\x00"
    header[60:64] = version.to_bytes(4, "big")
    return header


def test_accepts_prepared_working_database_without_writing(tmp_path):
    preflight = load_preflight()
    database = tmp_path / "anime.db"
    database.write_bytes(sqlite_header())
    before = database.read_bytes(), database.stat().st_mtime_ns
    preflight.require_prepared_database(database)
    assert (database.read_bytes(), database.stat().st_mtime_ns) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["anime.db"]


@pytest.mark.parametrize("contents", [None, b"", b"not sqlite", sqlite_header(0)])
def test_refuses_to_initialize_missing_empty_or_unversioned_database(tmp_path, contents):
    preflight = load_preflight()
    database = tmp_path / "anime.db"
    if contents is not None:
        database.write_bytes(contents)
    before = sorted(p.name for p in tmp_path.iterdir())
    with pytest.raises(preflight.PreflightError):
        preflight.require_prepared_database(database)
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    if contents is not None:
        assert database.read_bytes() == contents


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_refuses_linked_database_that_could_modify_a_reference(tmp_path, kind):
    preflight = load_preflight()
    reference = tmp_path / "reference.db"
    reference.write_bytes(sqlite_header())
    database = tmp_path / "anime.db"
    if kind == "symlink":
        database.symlink_to(reference)
    else:
        os.link(reference, database)
    with pytest.raises(preflight.PreflightError):
        preflight.require_prepared_database(database)
    assert reference.read_bytes() == sqlite_header()


def test_cli_stops_before_command_when_deployment_environment_is_wrong():
    assert SCRIPT.is_file(), "Nexus preflight script is missing"
    environment = {
        "DATABASE_URL": "sqlite:///wrong.db",
        "ANIME_PATH": "/media/anime",
        "REQUIRE_MOUNT": "true",
        "METADATA_ARTWORK_DIRECTORY": "/artwork",
    }
    result = subprocess.run(
        [sys.executable, "-B", str(SCRIPT), sys.executable, "-c", "print('command executed')"],
        env=environment, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 1
    assert "STOP" in result.stderr
    assert "Unexpected DATABASE_URL" in result.stderr
    assert "command executed" not in result.stdout


@pytest.fixture
def main_runtime(tmp_path, monkeypatch, capsys):
    preflight = load_preflight()
    database = tmp_path / "db" / "anime.db"
    database.parent.mkdir()
    database.write_bytes(sqlite_header())
    artwork = tmp_path / "artwork"
    artwork.mkdir()
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "40 20 0:44 / /media/anime ro,relatime - cifs //192.168.11.149/Anime ro,vers=3.0\n"
    )
    paths = {
        "/proc/self/mountinfo": mountinfo,
        "/data/anime.db": database,
        "/artwork": artwork,
    }
    # Redirect every guard path; an unexpected path fails instead of touching
    # the host. File/header/permission checks otherwise use the real helpers.
    monkeypatch.setattr(preflight, "Path", paths.__getitem__)
    runtime_os = SimpleNamespace(**vars(os))
    runtime_os.environ = {
        "DATABASE_URL": "sqlite:////data/anime.db",
        "ANIME_PATH": "/media/anime",
        "REQUIRE_MOUNT": "true",
        "METADATA_ARTWORK_DIRECTORY": "/artwork",
    }
    runtime_os.geteuid = lambda: 1000
    runtime_os.getegid = lambda: 1000
    exec_calls = []

    class ExecReached(Exception):
        pass

    def capture_exec(program, arguments):
        exec_calls.append((program, list(arguments)))
        raise ExecReached

    runtime_os.execvp = capture_exec
    monkeypatch.setattr(preflight, "os", runtime_os)
    monkeypatch.setattr(preflight.sys, "argv", [str(SCRIPT), "uvicorn", "app.main:app"])
    return SimpleNamespace(
        module=preflight, root=tmp_path, database=database, artwork=artwork,
        mountinfo=mountinfo, os=runtime_os, sys=preflight.sys,
        exec_calls=exec_calls, ExecReached=ExecReached,
    )


def assert_main_stops(runtime, capsys, reason):
    def snapshot():
        return {
            str(p.relative_to(runtime.root)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in runtime.root.rglob("*") if p.is_file()
        }

    before = snapshot()
    assert runtime.module.main() == 1
    output = capsys.readouterr()
    assert output.err.startswith("STOP: ")
    assert reason in output.err
    assert output.out == ""
    assert runtime.exec_calls == []
    assert snapshot() == before


@pytest.mark.parametrize("command", [
    ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"],
    ["python", "-c", "print('argument with spaces')"],
], ids=["uvicorn", "overridden-command"])
def test_main_executes_requested_command(main_runtime, capsys, command):
    runtime = main_runtime
    runtime.sys.argv[1:] = command
    before = runtime.database.read_bytes(), runtime.database.stat().st_mtime_ns
    with pytest.raises(runtime.ExecReached):
        runtime.module.main()
    assert runtime.exec_calls == [(command[0], command)]
    assert capsys.readouterr() == ("", "")
    assert (runtime.database.read_bytes(), runtime.database.stat().st_mtime_ns) == before


def test_main_accepts_readonly_container_over_rw_cifs_superblock(main_runtime, capsys):
    main_runtime.mountinfo.write_text(
        "40 20 0:44 / /media/anime ro - cifs //192.168.11.149/Anime rw\n"
    )
    with pytest.raises(main_runtime.ExecReached):
        main_runtime.module.main()
    assert main_runtime.exec_calls == [("uvicorn", ["uvicorn", "app.main:app"])]
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("fault", ["identity", "rw-alias"])
def test_main_stops_before_database_and_exec_for_unsafe_library(main_runtime, capsys, fault):
    mountinfo = "40 20 0:44 / /media/anime ro - cifs //192.168.11.149/Anime ro\n"
    if fault == "identity":
        mountinfo = mountinfo.replace("/Anime", "/Other")
    else:
        mountinfo += "41 20 0:44 / /alias rw - cifs //192.168.11.149/Anime rw\n"
    main_runtime.mountinfo.write_text(mountinfo)
    # A missing DB proves the mount error wins before DB validation or execution.
    main_runtime.database.unlink()
    assert_main_stops(main_runtime, capsys, "identity" if fault == "identity" else "writable")


@pytest.mark.parametrize("mountinfo,reason", [
    pytest.param(
        "20 1 259:2 / / ro - ext4 /dev/nvme0n1p2 ro\n",
        "one unambiguous CIFS mount", id="missing-cifs",
    ),
    pytest.param(
        "40 20 259:2 /mnt/nas-anime /media/anime ro - ext4 /dev/nvme0n1p2 ro\n",
        "active CIFS mount", id="readonly-ext4",
    ),
    pytest.param(
        "40 20 0:44 / /media/anime ro - autofs systemd-1 ro\n",
        "active CIFS mount", id="readonly-autofs",
    ),
])
def test_main_stops_for_missing_or_non_cifs_mount(main_runtime, capsys, mountinfo, reason):
    main_runtime.mountinfo.write_text(mountinfo)
    assert_main_stops(main_runtime, capsys, reason)


@pytest.mark.parametrize("mountinfo", [
    "40 20 0:44 / /media/anime rw - cifs //192.168.11.149/Anime ro\n",
], ids=["vfs-rw"])
def test_main_stops_for_writable_cifs(main_runtime, capsys, mountinfo):
    main_runtime.mountinfo.write_text(mountinfo)
    assert_main_stops(main_runtime, capsys, "container read-only")


@pytest.mark.parametrize("contents,reason", [
    pytest.param(None, "Prepared working DB is missing or unreadable", id="missing"),
    pytest.param(b"", "must already have a versioned SQLite schema", id="empty"),
    pytest.param(b"not sqlite", "must already have a versioned SQLite schema", id="invalid"),
    pytest.param(sqlite_header(0), "must already have a versioned SQLite schema", id="unversioned"),
])
def test_main_stops_for_missing_or_unprepared_database(main_runtime, capsys, contents, reason):
    if contents is None:
        main_runtime.database.unlink()
    else:
        main_runtime.database.write_bytes(contents)
    assert_main_stops(main_runtime, capsys, reason)


@pytest.mark.parametrize("denied", ["database", "database-directory", "artwork"])
def test_main_stops_for_denied_permissions(main_runtime, capsys, denied):
    runtime = main_runtime
    target = {
        "database": runtime.database,
        "database-directory": runtime.database.parent,
        "artwork": runtime.artwork,
    }[denied]
    real_access = runtime.os.access
    runtime.os.access = lambda path, mode: False if path == target else real_access(path, mode)
    reason = "Artwork bind directory" if denied == "artwork" else "Working DB and its directory"
    assert_main_stops(runtime, capsys, reason)


def test_main_stops_for_missing_artwork(main_runtime, capsys):
    main_runtime.artwork.rmdir()
    assert_main_stops(main_runtime, capsys, "Artwork bind directory")


@pytest.mark.parametrize("uid,gid", [(0, 1000), (1000, 0), (0, 0)])
def test_main_stops_for_root_runtime(main_runtime, capsys, uid, gid):
    main_runtime.os.geteuid = lambda: uid
    main_runtime.os.getegid = lambda: gid
    assert_main_stops(main_runtime, capsys, "non-root UID and GID")


def test_main_stops_for_missing_command(main_runtime, capsys):
    main_runtime.sys.argv[1:] = []
    assert_main_stops(main_runtime, capsys, "Missing application command")


@pytest.mark.parametrize("name", [
    "DATABASE_URL", "ANIME_PATH", "REQUIRE_MOUNT", "METADATA_ARTWORK_DIRECTORY",
])
def test_main_stops_for_wrong_environment(main_runtime, capsys, name):
    main_runtime.os.environ[name] = "unexpected"
    assert_main_stops(main_runtime, capsys, f"Unexpected {name}")
