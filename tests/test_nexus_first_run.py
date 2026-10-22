"""First-install lifecycle on temporary local storage; no NAS or production DB."""

import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tomllib
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from app.database import make_engine


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy/nexus/first_run.py"


def load_first_run():
    assert SCRIPT.is_file(), "Automatic first-install entrypoint is missing"
    spec = importlib.util.spec_from_file_location("nexus_first_run", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def storage(tmp_path):
    data, state = tmp_path / "db", tmp_path / "state"
    data.mkdir()
    state.mkdir()
    return data, state


def snapshot(root):
    return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in root.rglob("*") if p.is_file()}


def test_first_install_migrates_to_v9_and_restart_preserves_user_data(storage):
    first = load_first_run()
    data, state = storage
    first.provision(data, state)
    assert not (data / "anime.db").exists(), "Provisioning is evidence, not DB creation"
    with first.runtime_database(data, state) as url:
        engine = make_engine(url)
        with engine.begin() as connection:
            assert connection.scalar(text("PRAGMA user_version")) == 9
            assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
            assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
            assert connection.scalar(text("SELECT count(*) FROM sqlite_schema "
                                          "WHERE type='table' AND name NOT LIKE 'sqlite_%'")) == 18
            connection.execute(text("INSERT INTO videos "
                                    "(relative_path,root_folder,filename,size,mtime_ns) "
                                    "VALUES ('test.mkv','Test','preserved',1,1)"))
        engine.dispose()
    before = snapshot(data.parent)
    with first.runtime_database(data, state) as url:
        engine = make_engine(url)
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT filename FROM videos")) == "preserved"
        engine.dispose()
    assert snapshot(data.parent) == before


def test_missing_database_after_success_never_reinitializes(storage):
    first = load_first_run()
    data, state = storage
    first.provision(data, state)
    with first.runtime_database(data, state):
        pass
    (data / "anime.db").unlink()
    before = snapshot(data.parent)
    with pytest.raises(first.FirstRunError, match="missing|lost"):
        with first.runtime_database(data, state):
            pytest.fail("Lost DB admitted")
    assert snapshot(data.parent) == before
    with pytest.raises(first.FirstRunError):
        first.provision(data, state)
    assert not (data / "anime.db").exists()


def test_existing_only_connections_cannot_recreate_db_after_gate(storage, monkeypatch):
    first = load_first_run()
    data, state = storage
    first.provision(data, state)
    monkeypatch.chdir(data.parent)
    with first.runtime_database(data, state) as url:
        engine = make_engine(url)
        with engine.connect() as connection:
            assert connection.scalar(text("PRAGMA user_version")) == 9
        engine.dispose()  # Force a later pool connection after the loss.
        (data / "anime.db").unlink()
        with pytest.raises(OperationalError):
            with engine.connect():
                pytest.fail("SQLite recreated a lost DB")
        engine.dispose()
    assert not (data / "anime.db").exists()
    assert not (data.parent / "file:").exists(), "SQLite URI must not become a directory"


@pytest.mark.parametrize("contents", [None, b"", b"{}", b"not json"])
def test_missing_or_invalid_registration_never_creates_database(storage, contents):
    first = load_first_run()
    data, state = storage
    if contents is not None:
        (state / "provisioned.json").write_bytes(contents)
    before = snapshot(data.parent)
    with pytest.raises(first.FirstRunError):
        with first.runtime_database(data, state):
            pytest.fail("Unknown installation admitted")
    assert not (data / "anime.db").exists()
    assert snapshot(data.parent) == before


@pytest.mark.parametrize("name", ["anime.db", "anime.db-wal", "anime.db-shm", "anime.db-journal", "unexpected"])
def test_provision_refuses_nonempty_data(storage, name):
    first = load_first_run()
    data, state = storage
    (data / name).write_bytes(b"preserve")
    before = snapshot(data.parent)
    with pytest.raises(first.FirstRunError):
        first.provision(data, state)
    assert snapshot(data.parent) == before


def test_interrupted_migration_is_consumed_and_never_retried(storage, monkeypatch):
    first = load_first_run()
    from app import migrations
    data, state = storage
    first.provision(data, state)

    def interrupted(engine):
        raise RuntimeError("simulated migration interruption")

    with monkeypatch.context() as patch:
        patch.setattr(migrations, "migrate_schema_at_startup", interrupted)
        with pytest.raises(RuntimeError, match="interruption"):
            with first.runtime_database(data, state):
                pass
    before = snapshot(data.parent)
    with pytest.raises(first.FirstRunError, match="interrupted"):
        with first.runtime_database(data, state):
            pytest.fail("Interrupted bootstrap retried")
    assert snapshot(data.parent) == before


def test_failed_intent_fsync_prevents_database_creation(storage, monkeypatch):
    first = load_first_run()
    data, state = storage
    first.provision(data, state)

    def disk_failure(fd):
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(first.os, "fsync", disk_failure)
    with pytest.raises(OSError, match="fsync"):
        with first.runtime_database(data, state):
            pass
    assert not (data / "anime.db").exists()


def test_second_runtime_is_excluded(storage):
    first = load_first_run()
    data, state = storage
    first.provision(data, state)
    with first.runtime_database(data, state):
        with pytest.raises(first.FirstRunError, match="running|lock"):
            with first.runtime_database(data, state):
                pytest.fail("Concurrent instance admitted")


@pytest.mark.parametrize("fault", ["corrupt", "future-schema", "missing-table", "symlink", "hardlink"])
def test_invalid_ready_database_is_preserved_and_refused(storage, fault):
    first = load_first_run()
    data, state = storage
    first.provision(data, state)
    with first.runtime_database(data, state):
        pass
    database = data / "anime.db"
    if fault == "corrupt":
        database.write_bytes(b"not sqlite")
    elif fault in {"future-schema", "missing-table"}:
        with sqlite3.connect(database) as connection:
            connection.execute("PRAGMA user_version=10" if fault == "future-schema"
                               else "DROP TABLE videos")
    else:
        reference = data.parent / "reference.db"
        database.rename(reference)
        if fault == "symlink":
            database.symlink_to(reference)
        else:
            os.link(reference, database)
    before = snapshot(data.parent)
    with pytest.raises(first.FirstRunError):
        with first.runtime_database(data, state):
            pytest.fail("Invalid DB admitted")
    assert snapshot(data.parent) == before


def test_compose_exposes_single_app_on_exact_lan_address_and_preserves_isolation():
    import subprocess
    result = subprocess.run(
        ["docker", "compose", "--env-file", str(ROOT / "deploy/nexus/.env.example"),
         "-f", str(ROOT / "deploy/nexus/compose.yaml"), "config", "--format", "json"],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    config = json.loads(result.stdout)
    assert set(config["services"]) == {"app"}
    manifest = tomllib.loads((ROOT / "deploy/nexus/manifest.toml").read_text())
    assert manifest["services"]["docker"] == ["app"]
    assert manifest["services"]["ports"] == ["192.168.11.238:8000:8000/tcp"]
    assert manifest["paths"]["database"] == "/srv/nexus/appdata/anime-db/db/anime.db"
    assert manifest["paths"]["snapshots"] == [
        "/srv/nexus/appdata/anime-db/backups/daily",
        "/srv/nexus/appdata/anime-db/backups/pre-change",
    ]
    assert set(config["networks"]) == {"default"}
    app = config["services"]["app"]
    assert "profiles" not in app
    assert app["platform"] == "linux/arm64"
    database_url = make_url(app["environment"]["DATABASE_URL"])
    assert database_url.query == {"mode": "rw", "uri": "true"}
    assert len(app["ports"]) == 1
    assert app["ports"][0]["host_ip"] == "192.168.11.238"
    assert str(app["ports"][0]["published"]) == "8000"
    assert app["ports"][0]["target"] == 8000
    assert app["read_only"] and app["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in app["security_opt"]
    mounts = {m["target"]: m for m in app["volumes"]}
    assert set(mounts) == {"/data", "/state", "/artwork", "/media/anime"}
    assert mounts["/media/anime"]["read_only"] is True
    assert mounts["/media/anime"]["source"] == "/mnt/nas-anime"
    for target, leaf in [("/data", "db"), ("/state", "state"), ("/artwork", "artwork")]:
        assert mounts[target]["source"] == f"/srv/nexus/appdata/anime-db/{leaf}"
        assert mounts[target]["bind"]["create_host_path"] is False
    for name in ["METADATA_ENABLED", "ANILIST_ENABLED", "METADATA_DOWNLOAD_ARTWORK",
                 "METADATA_ALLOW_REMOTE_IMAGES", "METADATA_AUTO_CONFIRM"]:
        assert app["environment"][name] == "false"


@pytest.fixture
def launcher(storage, tmp_path, monkeypatch):
    first = load_first_run()
    spec = importlib.util.spec_from_file_location("nexus_preflight", ROOT / "deploy/nexus/preflight.py")
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    monkeypatch.setitem(sys.modules, "nexus_preflight", guard)
    data, state = storage
    first.provision(data, state)
    artwork = tmp_path / "artwork"
    artwork.mkdir()
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "20 1 0:30 / / ro - overlay overlay rw\n"
        "40 20 0:44 / /media/anime ro - cifs //192.168.11.149/Anime rw\n"
        "41 20 259:2 /srv/nexus/appdata/anime-db/db /data rw - ext4 /dev/nvme0n1p2 rw\n"
        "42 20 259:2 /srv/nexus/appdata/anime-db/state /state rw - ext4 /dev/nvme0n1p2 rw\n"
        "43 20 259:2 /srv/nexus/appdata/anime-db/artwork /artwork rw - ext4 /dev/nvme0n1p2 rw\n"
    )
    paths = {"/proc/self/mountinfo": mountinfo, "/data/anime.db": data / "anime.db", "/artwork": artwork}
    monkeypatch.setattr(first, "Path", paths.__getitem__)
    runtime_os = SimpleNamespace(**vars(os))
    runtime_os.environ = {
        "DATABASE_URL": "sqlite:///file:///data/anime.db?mode=rw&uri=true",
        "ANIME_PATH": "/media/anime",
        "REQUIRE_MOUNT": "true", "METADATA_ARTWORK_DIRECTORY": "/artwork",
        **{name: "false" for name in (
            "METADATA_ENABLED", "ANILIST_ENABLED", "METADATA_DOWNLOAD_ARTWORK",
            "METADATA_ALLOW_REMOTE_IMAGES", "METADATA_AUTO_CONFIRM",
        )},
    }
    runtime_os.geteuid = runtime_os.getegid = lambda: 1000

    class ExecReached(Exception):
        pass

    def execvp(program, argv):
        assert program == "uvicorn" and argv[1] == "app.main:app"
        assert "mode=rw&uri=true" in runtime_os.environ["DATABASE_URL"]
        raise ExecReached

    runtime_os.execvp = execvp
    monkeypatch.setattr(first, "os", runtime_os)
    original_runtime = first.runtime_database
    monkeypatch.setattr(first, "runtime_database", lambda: original_runtime(data, state))
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "uvicorn", "app.main:app"])
    return SimpleNamespace(first=first, data=data, state=state, mountinfo=mountinfo,
                           os=runtime_os, ExecReached=ExecReached)


def test_launcher_creates_database_before_exec_and_sets_existing_only_url(launcher):
    with pytest.raises(launcher.ExecReached):
        launcher.first.main()
    assert (launcher.state / "ready.json").is_file()


def test_launcher_reports_stop_for_unwritable_ready_database(launcher, monkeypatch, capsys):
    with launcher.first.runtime_database():
        pass
    guard = sys.modules["nexus_preflight"]
    guard_os = SimpleNamespace(**vars(os))
    guard_os.access = lambda path, mode: path != launcher.data / "anime.db"
    monkeypatch.setattr(guard, "os", guard_os)
    before = snapshot(launcher.data.parent)
    assert launcher.first.main() == 1
    assert capsys.readouterr().err.startswith("STOP:")
    assert snapshot(launcher.data.parent) == before


@pytest.mark.parametrize("fault", ["cifs", "nvme", "metadata", "root", "command"])
def test_launcher_rejects_unsafe_runtime_before_initialization(launcher, capsys, fault):
    if fault in {"cifs", "nvme"}:
        raw = launcher.mountinfo.read_text()
        launcher.mountinfo.write_text(raw.replace("/Anime", "/Other") if fault == "cifs"
                                      else raw.replace("/dev/nvme0n1p2", "/dev/mmcblk0p2"))
    elif fault == "metadata":
        launcher.os.environ["METADATA_ENABLED"] = "true"
    elif fault == "root":
        launcher.os.geteuid = lambda: 0
    else:
        sys.argv[1:] = []
    before = snapshot(launcher.state)
    assert launcher.first.main() == 1
    assert capsys.readouterr().err.startswith("STOP:")
    assert snapshot(launcher.state) == before
    assert not (launcher.data / "anime.db").exists()


def test_real_application_start_stop_start_on_clean_v9_without_scan(storage, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.config import Settings
    from app.main import create_app
    from app import main

    first = load_first_run()
    data, state = storage
    library = tmp_path / "library"
    library.mkdir()
    artwork = tmp_path / "artwork"
    artwork.mkdir()

    def no_scan(*args, **kwargs):
        pytest.fail("Startup/smoke must never invoke the scanner")

    monkeypatch.setattr(main, "scan_library", no_scan)
    first.provision(data, state)
    for _ in range(2):
        with first.runtime_database(data, state) as url:
            settings = Settings(anime_path=library, database_url=url,
                                metadata_artwork_directory=artwork, metadata_enabled=False,
                                anilist_enabled=False, metadata_download_artwork=False,
                                metadata_allow_remote_images=False)
            before = snapshot(data)
            with TestClient(create_app(settings)) as client:
                assert client.get("/health").json() == {"status": "ok"}
                assert client.get("/").status_code == 200
            assert snapshot(data) == before
