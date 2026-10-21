"""Standalone backup contract; all SQLite and filesystem writes are under tmp_path."""

import ast
import errno
import fcntl
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import signal
import subprocess
import sys
import time

import pytest
from sqlalchemy import create_engine

from app.database import Base
from app import models  # Register the real schema; no application/startup import.


pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Nexus helper targets Linux")
REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "deploy/nexus/scripts/sqlite_backup.py"
MIGRATIONS = ast.parse((REPO / "app/migrations.py").read_text())
SCHEMA_VERSION = next(
    node.value.value for node in MIGRATIONS.body
    if isinstance(node, ast.Assign)
    and any(isinstance(t, ast.Name) and t.id == "STARTUP_COMPATIBILITY_VERSION" for t in node.targets)
)


def load_helper():
    assert SCRIPT.is_file(), "N4A SQLite backup helper is missing"
    spec = importlib.util.spec_from_file_location("nexus_sqlite_backup", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    helper = load_helper()
    root = tmp_path / "appdata"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(helper, "APPDATA_ROOT", root)
    return helper, root


def make_database(root, instance="A"):
    path = root / instance / "db" / "anime.db"
    path.parent.mkdir(parents=True, mode=0o700)
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql(f"PRAGMA user_version={SCHEMA_VERSION}")
        connection.execute(models.CatalogCollection.__table__.insert().values(
            id=1, local_title=instance, normalized_local_title=instance.lower(), relative_root_path=instance,
        ))
    engine.dispose()
    return path


def published(root, instance):
    directory = root / "backups" / instance
    return sorted(directory.iterdir()) if directory.exists() else []


def check_snapshot(result, instance, root):
    assert result["instance"] == instance
    assert result["status"] == "SUCCESS"
    assert result["remote_backup_verified"] is False
    generation = Path(result["snapshot"])
    assert generation.parent == root / "backups" / instance
    assert sorted(p.name for p in generation.iterdir()) == ["SHA256SUMS", "anime.db", "snapshot.json"]
    metadata = json.loads((generation / "snapshot.json").read_text())
    assert metadata["instance"] == instance
    assert metadata["schema_version"] == SCHEMA_VERSION
    assert metadata["sqlite_version"] == sqlite3.sqlite_version
    assert metadata["git_revision"] is None
    assert metadata["remote_backup_verified"] is False
    assert metadata["copy_confinement"]["landlock_abi"] >= 3
    assert metadata["source"]["path"] == str(root / instance / "db" / "anime.db")
    assert metadata["started_at"] <= metadata["completed_at"]
    assert metadata["database_size"] == (generation / "anime.db").stat().st_size
    assert metadata["database_sha256"] == sha256((generation / "anime.db").read_bytes()).hexdigest()
    assert metadata["validation"]["integrity_check"] == "ok"
    assert metadata["validation"]["foreign_key_check"] == []
    assert metadata["validation"]["standalone"] is True
    assert metadata["validation"]["tables"] == sorted(Base.metadata.tables)
    sums = {}
    for line in (generation / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split("  ")
        sums[name] = digest
    assert set(sums) == {"anime.db", "snapshot.json"}
    for name, digest in sums.items():
        assert sha256((generation / name).read_bytes()).hexdigest() == digest
    with sqlite3.connect(f"file:{generation / 'anime.db'}?mode=ro", uri=True) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        assert connection.execute("SELECT local_title FROM catalog_collections").fetchone() == (instance,)
    assert (generation / "anime.db").stat().st_nlink == 1
    return generation


def test_a_only_snapshot_leaves_inactive_b_absent(runtime):
    helper, root = runtime
    source = make_database(root)
    before = source.read_bytes(), source.stat().st_mtime_ns
    report = helper.backup_instances(["A"])
    assert report["success"] is True
    assert report["expected_instances"] == ["A"]
    assert len(report["results"]) == 1
    check_snapshot(report["results"][0], "A", root)
    assert not (root / "B").exists()
    assert not (root / "backups" / "B").exists()
    assert (source.read_bytes(), source.stat().st_mtime_ns) == before


def test_independent_a_and_b_snapshots(runtime):
    helper, root = runtime
    make_database(root, "A")
    make_database(root, "B")
    report = helper.backup_instances(["A", "B"])
    assert report["success"] is True
    assert [r["instance"] for r in report["results"]] == ["A", "B"]
    for result in report["results"]:
        check_snapshot(result, result["instance"], root)


def test_expected_missing_b_is_a_machine_readable_failure(runtime):
    helper, root = runtime
    make_database(root)
    report = helper.backup_instances(["A", "B"])
    assert report["success"] is False
    check_snapshot(report["results"][0], "A", root)
    assert report["results"][1]["instance"] == "B"
    assert report["results"][1]["status"] == "STOP"
    assert report["results"][1]["code"] == "MISSING_SOURCE"
    assert published(root, "B") == []
    assert not (root / "B").exists()


@pytest.mark.parametrize("fault,code", [
    ("marker", "SCHEMA"), ("tables", "SCHEMA"), ("corrupt", "SQLITE"),
    ("foreign-key", "VALIDATION"),
])
def test_invalid_database_is_never_published(runtime, fault, code):
    helper, root = runtime
    database = make_database(root)
    if fault == "corrupt":
        database.write_bytes(b"not a SQLite database" * 100)
    else:
        with sqlite3.connect(database) as connection:
            if fault == "marker":
                connection.execute("PRAGMA user_version=0")
            elif fault == "tables":
                connection.execute("DROP TABLE artwork")
            else:
                connection.execute(
                    "INSERT INTO audio_tracks (video_id,stream_index,language) VALUES (999,0,'ja')"
                )
    before = database.read_bytes(), database.stat().st_mtime_ns
    report = helper.backup_instances(["A"])
    assert report["success"] is False
    assert report["results"][0]["status"] == "STOP"
    assert report["results"][0]["code"] == code
    assert published(root, "A") == []
    assert (database.read_bytes(), database.stat().st_mtime_ns) == before


def test_source_is_opened_read_only_independently_of_the_sandbox(runtime):
    helper, root = runtime
    database = make_database(root)
    before = database.read_bytes(), database.stat().st_mtime_ns
    directory = os.open(database.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        uri = helper._uri(directory, readonly=True)
        assert uri.endswith("?mode=ro")
        assert "immutable" not in uri
        assert helper._uri(directory, readonly=False).endswith("?mode=rw")
        # Landlock is not enforced here, so only mode=ro can refuse this write.
        connection = sqlite3.connect(uri, uri=True)
        try:
            with pytest.raises(sqlite3.OperationalError):
                connection.execute("UPDATE catalog_collections SET local_title='written'")
        finally:
            connection.close()
    finally:
        os.close(directory)
    assert (database.read_bytes(), database.stat().st_mtime_ns) == before


def test_wal_commits_are_copied_without_checkpointing_source(runtime):
    helper, root = runtime
    database = make_database(root)
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("UPDATE catalog_collections SET local_title='A from WAL'")
        connection.commit()
        before = database.read_bytes(), database.stat().st_mtime_ns
        wal = Path(str(database) + "-wal")
        wal_before = wal.read_bytes(), wal.stat().st_mtime_ns
        report = helper.backup_instances(["A"])
        assert report["success"] is True
        generation = Path(report["results"][0]["snapshot"])
        with sqlite3.connect(generation / "anime.db") as restored:
            assert restored.execute("SELECT local_title FROM catalog_collections").fetchone() == ("A from WAL",)
            assert restored.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        assert (database.read_bytes(), database.stat().st_mtime_ns) == before
        assert (wal.read_bytes(), wal.stat().st_mtime_ns) == wal_before
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert not any(p.name.endswith(("-wal", "-shm", "-journal")) for p in generation.iterdir())
    finally:
        connection.close()


def test_copy_is_consistent_during_concurrent_wal_writes(runtime):
    helper, root = runtime
    database = make_database(root)
    with sqlite3.connect(database) as connection:
        connection.executescript(
            "PRAGMA journal_mode=WAL; CREATE TABLE counter (id INTEGER PRIMARY KEY, value INTEGER);"
            "INSERT INTO counter VALUES (1,0),(2,0);"
        )
    # A separate real process keeps paired updates atomic while the helper reads.
    code = """
import sqlite3,sys,time
c=sqlite3.connect(sys.argv[1]); c.execute('PRAGMA wal_autocheckpoint=0')
for n in range(1,100000):
    with c: c.execute('UPDATE counter SET value=?',(n,))
    if n==1: print('ready',flush=True)
    time.sleep(.001)
"""
    writer = subprocess.Popen([sys.executable, "-B", "-c", code, str(database)], stdout=subprocess.PIPE, text=True)
    try:
        assert writer.stdout.readline().strip() == "ready"
        report = helper.backup_instances(["A"], timeout=5)
        assert report["success"] is True
        with sqlite3.connect(Path(report["results"][0]["snapshot"]) / "anime.db") as copy:
            rows = copy.execute("SELECT value FROM counter ORDER BY id").fetchall()
            assert len(rows) == 2 and rows[0] == rows[1] and rows[0][0] > 0
            assert copy.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert writer.poll() is None
    finally:
        writer.terminate()
        writer.wait(timeout=5)


def test_wal_source_without_shm_stops_without_writing_a_sidecar(runtime):
    helper, root = runtime
    database = make_database(root)
    # A hard crash leaves real uncheckpointed frames and no live wal-index owner.
    code = """
import os,sqlite3,sys
c=sqlite3.connect(sys.argv[1])
c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA wal_autocheckpoint=0')
c.execute("UPDATE catalog_collections SET local_title='uncheckpointed'"); c.commit()
os.kill(os.getpid(), 9)
"""
    crashed = subprocess.run([sys.executable, "-B", "-c", code, str(database)], timeout=30)
    assert crashed.returncode == -signal.SIGKILL
    wal, shm = Path(str(database) + "-wal"), Path(str(database) + "-shm")
    assert wal.stat().st_size > 0 and shm.exists()
    shm.unlink()
    before = database.read_bytes(), database.stat().st_mtime_ns
    wal_before = wal.read_bytes(), wal.stat().st_mtime_ns
    report = helper.backup_instances(["A"])
    assert report["success"] is False
    assert report["results"][0]["status"] == "STOP"
    assert report["results"][0]["code"] == "SQLITE"
    assert published(root, "A") == []
    assert (database.read_bytes(), database.stat().st_mtime_ns) == before
    assert (wal.read_bytes(), wal.stat().st_mtime_ns) == wal_before
    # Recreating the source wal-index would be an unpermitted source write.
    assert sorted(p.name for p in database.parent.iterdir()) == ["anime.db", "anime.db-wal"]


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "parent-symlink", "sidecar-symlink", "sidecar-hardlink"])
def test_source_aliases_are_rejected(runtime, kind):
    helper, root = runtime
    database = make_database(root)
    reference = root / "reference.db"
    if kind.startswith("sidecar"):
        reference.write_bytes(b"protected sidecar target")
        sidecar = Path(str(database) + "-shm")
        if kind == "sidecar-symlink":
            sidecar.symlink_to(reference)
        else:
            os.link(reference, sidecar)
    elif kind == "parent-symlink":
        database.parent.rename(root / "real-db")
        (root / "A" / "db").symlink_to(root / "real-db", target_is_directory=True)
        reference = root / "real-db" / "anime.db"
    else:
        database.rename(reference)
        if kind == "symlink":
            database.symlink_to(reference)
        else:
            os.link(reference, database)
    before = reference.read_bytes(), reference.stat().st_mtime_ns
    report = helper.backup_instances(["A"])
    assert report["success"] is False
    assert report["results"][0]["code"] == "UNSAFE_PATH"
    assert published(root, "A") == []
    assert (reference.read_bytes(), reference.stat().st_mtime_ns) == before


@pytest.mark.parametrize("target", ["A", ".partial"])
def test_output_symlink_cannot_write_into_other_instance(runtime, target):
    helper, root = runtime
    make_database(root)
    backups = root / "backups"
    backups.mkdir()
    other = root / "protected"
    other.mkdir()
    (other / "sentinel").write_bytes(b"untouched")
    (backups / target).symlink_to(other, target_is_directory=True)
    report = helper.backup_instances(["A"])
    assert report["success"] is False
    assert report["results"][0]["code"] == "UNSAFE_PATH"
    assert sorted(p.name for p in other.iterdir()) == ["sentinel"]
    assert (other / "sentinel").read_bytes() == b"untouched"


@pytest.mark.parametrize("target", ["source", "output"])
def test_real_permission_denial_stops_the_instance(runtime, target):
    helper, root = runtime
    database = make_database(root)
    if target == "output":
        denied = root / "backups"
        denied.mkdir(mode=0o700)
        denied.chmod(0o500)
    else:
        denied = database
        denied.chmod(0)
    try:
        report = helper.backup_instances(["A"])
        assert report["success"] is False
        assert report["results"][0]["code"] == "PERMISSION"
        assert published(root, "A") == []
    finally:
        denied.chmod(0o700 if target == "output" else 0o600)


@pytest.mark.parametrize("target,mode", [
    ("root", 0o770), ("root", 0o707), ("backups", 0o770), ("backups", 0o707),
])
def test_group_or_other_writable_directory_is_rejected(runtime, target, mode):
    helper, root = runtime
    make_database(root)
    directory = root if target == "root" else root / "backups"
    if target == "backups":
        directory.mkdir(mode=0o700)
    directory.chmod(mode)
    try:
        report = helper.backup_instances(["A"])
        assert report["success"] is False
        assert report["results"][0]["code"] == "UNSAFE_PATH"
        assert published(root, "A") == []
    finally:
        directory.chmod(0o700)


@pytest.mark.parametrize("mode", [0o640, 0o604])
def test_group_or_other_accessible_snapshot_file_is_rejected(runtime, monkeypatch, mode):
    helper, root = runtime
    make_database(root)
    reached = root / "widened-mode-reached"
    original = helper._validate_copy
    def widen(work_fd):
        result = original(work_fd)
        reached.touch()
        os.chmod("anime.db", mode, dir_fd=work_fd)
        return result
    monkeypatch.setattr(helper, "_validate_copy", widen)
    report = helper.backup_instances(["A"])
    assert report["success"] is False
    assert report["results"][0]["code"] == "UNSAFE_PATH"
    assert published(root, "A") == []
    assert reached.exists(), report


@pytest.mark.parametrize("instances", [[], ["A", "A"], ["../B"], ["/tmp"], ["C"]])
def test_instances_are_explicit_validated_authority(runtime, instances):
    helper, root = runtime
    with pytest.raises(ValueError):
        helper.backup_instances(instances)
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_invalid_deadline_is_rejected_before_any_write(runtime, timeout):
    helper, root = runtime
    with pytest.raises(ValueError):
        helper.backup_instances(["A"], timeout=timeout)
    assert list(root.iterdir()) == []


def test_generation_collision_never_overwrites_an_older_snapshot(runtime, monkeypatch):
    helper, root = runtime
    make_database(root)
    first = helper.backup_instances(["A"])
    directory = check_snapshot(first["results"][0], "A", root)
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in directory.iterdir()}
    monkeypatch.setattr(helper, "_generation_id", lambda: directory.name)
    second = helper.backup_instances(["A"])
    assert second["success"] is False
    assert second["results"][0]["code"] == "COLLISION"
    assert published(root, "A") == [directory]
    assert {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in directory.iterdir()} == before


def test_blocked_sqlite_backup_has_a_real_process_deadline(runtime):
    helper, root = runtime
    database = make_database(root)
    locker = sqlite3.connect(database)
    try:
        locker.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        report = helper.backup_instances(["A"], timeout=0.25)
        assert time.monotonic() - started < 4
        assert report["success"] is False
        assert report["results"][0]["code"] == "TIMEOUT"
        assert published(root, "A") == []
    finally:
        locker.rollback()
        locker.close()


@pytest.mark.parametrize("phase", ["rename", "parent-fsync", "metadata"])
def test_publication_failure_leaves_no_successful_generation(runtime, monkeypatch, phase):
    helper, root = runtime
    make_database(root)
    destination = root / "backups" / "A"
    destination.parent.mkdir(mode=0o700)
    destination.mkdir(mode=0o700)
    reached = root / "injected-fault-reached"
    if phase == "rename":
        def failed_rename(*args):
            reached.touch()
            raise OSError(errno.EIO, "simulated rename failure")
        monkeypatch.setattr(helper, "_rename_noreplace", failed_rename)
    elif phase == "parent-fsync":
        inode = destination.stat().st_ino
        real_fsync = os.fsync
        def failed_fsync(fd):
            if os.fstat(fd).st_ino == inode:
                reached.touch()
                raise OSError(errno.EIO, "simulated directory fsync failure")
            return real_fsync(fd)
        monkeypatch.setattr(helper.os, "fsync", failed_fsync)
    else:
        original = helper._write_metadata
        def damage_metadata(*args):
            original(*args)
            reached.touch()
            fd = os.open("snapshot.json", os.O_WRONLY | os.O_APPEND, dir_fd=args[0])
            with os.fdopen(fd, "wb") as handle:
                handle.write(b"corrupt")
        monkeypatch.setattr(helper, "_write_metadata", damage_metadata)
    report = helper.backup_instances(["A"])
    assert report["success"] is False
    assert report["results"][0]["status"] == "STOP"
    assert published(root, "A") == []
    assert reached.exists(), report


def test_failed_a_does_not_prevent_or_impersonate_successful_b(runtime):
    helper, root = runtime
    make_database(root, "B")
    report = helper.backup_instances(["A", "B"])
    assert report["success"] is False
    assert report["results"][0]["instance"] == "A"
    assert report["results"][0]["code"] == "MISSING_SOURCE"
    check_snapshot(report["results"][1], "B", root)


def test_a_only_does_not_touch_an_existing_b(runtime):
    helper, root = runtime
    make_database(root, "A")
    b = make_database(root, "B")
    before = b.read_bytes(), b.stat().st_mtime_ns
    assert helper.backup_instances(["A"])["success"] is True
    assert (b.read_bytes(), b.stat().st_mtime_ns) == before
    assert not (root / "backups" / "B").exists()


def test_repeated_success_preserves_all_generations_without_retention(runtime):
    helper, root = runtime
    make_database(root)
    first = helper.backup_instances(["A"])
    first_dir = check_snapshot(first["results"][0], "A", root)
    before = {p.name: p.read_bytes() for p in first_dir.iterdir()}
    second = helper.backup_instances(["A"])
    check_snapshot(second["results"][0], "A", root)
    assert len(published(root, "A")) == 2
    assert {p.name: p.read_bytes() for p in first_dir.iterdir()} == before


@pytest.mark.parametrize("argv", [[], ["--instances", "A", "--root", "/mnt/nas-anime"], ["--instances", "../B"]])
def test_cli_does_not_accept_arbitrary_root_or_implicit_instances(argv):
    assert SCRIPT.is_file(), "N4A SQLite backup helper is missing"
    result = subprocess.run([sys.executable, "-B", str(SCRIPT), *argv], capture_output=True, text=True, timeout=5)
    assert result.returncode == 2
    assert result.stdout == ""


def test_cli_exit_status_and_json_describe_expected_missing_instance(runtime, capsys):
    helper, root = runtime
    make_database(root)
    assert helper.main(["--instances", "A", "B"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["success"] is False
    assert [r["status"] for r in report["results"]] == ["SUCCESS", "STOP"]


@pytest.mark.parametrize("table", sorted(Base.metadata.tables))
def test_every_current_application_table_is_required(runtime, table):
    helper, root = runtime
    database = make_database(root)
    with sqlite3.connect(database) as connection:
        connection.execute(f'DROP TABLE "{table}"')
    report = helper.backup_instances(["A"])
    assert report["results"][0]["code"] == "SCHEMA"
    assert published(root, "A") == []


def test_worker_start_failure_is_machine_readable_and_does_not_leak_fds(runtime, monkeypatch):
    helper, root = runtime
    make_database(root)
    def cannot_fork():
        raise OSError(errno.EAGAIN, "No process slots")
    monkeypatch.setattr(helper.os, "fork", cannot_fork)
    before = set(Path("/proc/self/fd").iterdir())
    report = helper.backup_instances(["A", "B"])
    assert report["success"] is False
    assert [r["instance"] for r in report["results"]] == ["A", "B"]
    assert all(r["status"] == "STOP" for r in report["results"])
    assert set(Path("/proc/self/fd").iterdir()) == before
    assert published(root, "A") == []


def test_worker_crash_cannot_be_reported_as_a_snapshot(runtime, monkeypatch):
    helper, root = runtime
    make_database(root)
    monkeypatch.setattr(helper, "_copy_database", lambda *_: os._exit(9))
    report = helper.backup_instances(["A"])
    assert report["results"][0]["code"] == "WORKER_FAILED"
    assert published(root, "A") == []


def test_timeout_also_covers_publication_and_withdraws_the_generation(runtime, monkeypatch):
    helper, root = runtime
    make_database(root)
    destination = root / "backups" / "A"
    destination.parent.mkdir(mode=0o700)
    destination.mkdir(mode=0o700)
    inode = destination.stat().st_ino
    parent = os.getpid()
    real_fsync = os.fsync
    def stuck_commit(fd):
        if os.getpid() != parent and os.fstat(fd).st_ino == inode and os.listdir(fd):
            (root / "publication-started").touch()
            time.sleep(20)
        return real_fsync(fd)
    monkeypatch.setattr(helper.os, "fsync", stuck_commit)
    started = time.monotonic()
    report = helper.backup_instances(["A"], timeout=0.4)
    assert time.monotonic() - started < 4
    assert report["results"][0]["code"] == "TIMEOUT", report
    assert published(root, "A") == []
    assert (root / "publication-started").exists()


def test_supervisor_never_reports_success_after_the_deadline(runtime, monkeypatch):
    helper, root = runtime
    make_database(root)
    real_wait = os.waitpid
    delayed = False
    def delayed_wait(pid, options):
        nonlocal delayed
        if options == os.WNOHANG and not delayed:
            delayed = True
            time.sleep(.25)
        return real_wait(pid, options)
    monkeypatch.setattr(helper.os, "waitpid", delayed_wait)
    report = helper.backup_instances(["A"], timeout=.1)
    assert report["success"] is False
    assert report["results"][0]["code"] == "TIMEOUT"
    assert published(root, "A") == []


def test_sidecar_swap_between_checks_cannot_modify_another_file(runtime, monkeypatch):
    helper, root = runtime
    database = make_database(root)
    code = """
import sqlite3,sys,time
c=sqlite3.connect(sys.argv[1]); c.execute('PRAGMA journal_mode=WAL')
c.execute('CREATE TABLE wal_data(id)'); c.commit(); print('ready',flush=True)
time.sleep(30)
"""
    writer = subprocess.Popen([sys.executable, "-B", "-c", code, str(database)], stdout=subprocess.PIPE, text=True)
    try:
        assert writer.stdout.readline().strip() == "ready"
        victim = root / "protected-victim"
        victim.write_bytes(b"PROTECTED" * 4096)
        before = victim.read_bytes(), victim.stat().st_mtime_ns
        shm = Path(str(database) + "-shm")
        saved = shm.with_name("saved-shm")
        original = helper._copy_database
        def attack(*args):
            shm.rename(saved)
            os.link(victim, shm)
            try:
                return original(*args)
            finally:
                shm.unlink()
                saved.rename(shm)
        monkeypatch.setattr(helper, "_copy_database", attack)
        report = helper.backup_instances(["A"])
        assert (victim.read_bytes(), victim.stat().st_mtime_ns) == before
        assert report["success"] is False
        assert published(root, "A") == []
    finally:
        writer.terminate()
        writer.wait(timeout=5)


def test_temporary_source_replacement_cannot_impersonate_the_pinned_database(runtime, monkeypatch):
    helper, root = runtime
    database = make_database(root)
    other = make_database(root, "B")
    original = helper._copy_database
    def swap_for_copy(*args):
        saved = database.with_name("saved.db")
        database.rename(saved)
        database.write_bytes(other.read_bytes())
        try:
            return original(*args)
        finally:
            database.unlink()
            saved.rename(database)
    monkeypatch.setattr(helper, "_copy_database", swap_for_copy)
    report = helper.backup_instances(["A"])
    assert report["success"] is False
    assert published(root, "A") == []


@pytest.mark.parametrize("file_kind", ["database", "sidecar"])
def test_file_mount_alias_is_rejected(runtime, monkeypatch, file_kind):
    helper, root = runtime
    database = make_database(root)
    target = database
    if file_kind == "sidecar":
        target = Path(str(database) + "-journal")
        target.write_bytes(b"")
    inode = target.stat().st_ino
    original = helper._mount_id
    monkeypatch.setattr(helper, "_mount_id", lambda fd: -1 if os.fstat(fd).st_ino == inode else original(fd))
    report = helper.backup_instances(["A"])
    assert report["results"][0]["status"] == "STOP"
    assert report["results"][0]["code"] == "UNSAFE_PATH"
    assert published(root, "A") == []


def test_sigterm_during_supervisor_setup_still_returns_json_and_reaps_child(runtime, monkeypatch, capsys):
    helper, root = runtime
    make_database(root)
    real_close = os.close
    parent = os.getpid()
    fired = False
    def interrupt_close(fd):
        nonlocal fired
        if os.getpid() == parent and not fired:
            fired = True
            os.kill(parent, signal.SIGTERM)
        return real_close(fd)
    monkeypatch.setattr(helper.os, "close", interrupt_close)
    assert helper.main(["--instances", "A", "B"]) == 130
    report = json.loads(capsys.readouterr().out)
    assert [r["code"] for r in report["results"]] == ["INTERRUPTED", "INTERRUPTED"]
    assert published(root, "A") == []


def test_interruption_before_worker_setup_is_still_machine_readable(runtime, monkeypatch, capsys):
    helper, root = runtime
    def interrupt_generation():
        raise KeyboardInterrupt
    monkeypatch.setattr(helper, "_generation_id", interrupt_generation)
    try:
        exit_code = helper.main(["--instances", "A", "B"])
    except KeyboardInterrupt:
        pytest.fail("Interruption escaped without per-instance JSON")
    assert exit_code == 130
    report = json.loads(capsys.readouterr().out)
    assert [r["code"] for r in report["results"]] == ["INTERRUPTED", "INTERRUPTED"]
    assert list(root.iterdir()) == []


def test_kernel_confinement_denies_source_writes_and_unapproved_reads(tmp_path):
    source = tmp_path / "source"
    source.write_text("readable source")
    victim = tmp_path / "victim"
    victim.write_text("protected")
    alias = tmp_path / "hardlink-to-victim"
    os.link(victim, alias)
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    code = """
import runpy,os,sys,json
m=runpy.run_path(sys.argv[1])
work,source,victim,alias=sys.argv[2:]
w=os.open(work,os.O_RDONLY|os.O_DIRECTORY)
s=os.open(source,os.O_RDONLY)
abi=m['_confine_copy'](w,[s])
assert open(source).read()=='readable source'
denied=[]
for name,mode in [(source,'r+b'),(victim,'rb'),(alias,'r+b')]:
    try: open(name,mode)
    except PermissionError: denied.append(True)
    else: denied.append(False)
with open(work+'/allowed','w') as out: out.write('confined output')
print(json.dumps({'abi':abi,'denied':denied}))
"""
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code, str(SCRIPT), str(work), str(source), str(victim), str(alias)],
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["denied"] == [True, True, True]
    assert (work / "allowed").read_text() == "confined output"
    assert source.read_text() == "readable source"
    assert victim.read_text() == "protected"


def test_unavailable_landlock_is_a_stop_without_unsafe_fallback(runtime, monkeypatch):
    helper, root = runtime
    class UnavailableCall:
        def __call__(self, number, *args):
            assert number == 444 and args[-1].value == 1
            return -1
    class UnavailableLibc:
        syscall = UnavailableCall()
    monkeypatch.setattr(helper.ctypes, "CDLL", lambda *args, **kwargs: UnavailableLibc())
    with pytest.raises(helper.BackupError) as error:
        helper._confine_copy(-1, [])
    assert error.value.code == "SANDBOX_UNAVAILABLE"
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("filesystem", ["cifs", "nfs", "autofs"])
def test_remote_or_automount_root_is_rejected_before_writes(runtime, monkeypatch, filesystem):
    helper, root = runtime
    make_database(root)
    monkeypatch.setattr(helper, "_mount_type", lambda _: filesystem)
    report = helper.backup_instances(["A"])
    assert report["results"][0]["code"] == "UNSAFE_PATH"
    assert not (root / "backups").exists()


def test_nested_mount_alias_is_rejected(runtime, monkeypatch):
    helper, root = runtime
    make_database(root)
    source_inode = (root / "A" / "db").stat().st_ino
    original = helper._mount_id
    monkeypatch.setattr(helper, "_mount_id", lambda fd: -1 if os.fstat(fd).st_ino == source_inode else original(fd))
    report = helper.backup_instances(["A"])
    assert report["results"][0]["code"] == "UNSAFE_PATH"
    assert published(root, "A") == []


def test_existing_helper_lock_is_not_bypassed(runtime):
    helper, root = runtime
    make_database(root)
    backups = root / "backups"
    backups.mkdir(mode=0o700)
    lock = os.open(backups / ".sqlite-backup.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        report = helper.backup_instances(["A"])
        assert report["results"][0]["code"] == "LOCKED"
        assert published(root, "A") == []
    finally:
        os.close(lock)


def test_source_replacement_during_copy_is_not_published(runtime, monkeypatch):
    helper, root = runtime
    database = make_database(root)
    original = helper._copy_database
    def replace_source(*args):
        original(*args)
        database.rename(database.with_name("original.db"))
        database.write_bytes(b"a different file")
    monkeypatch.setattr(helper, "_copy_database", replace_source)
    report = helper.backup_instances(["A"])
    assert report["results"][0]["code"] == "UNSAFE_PATH"
    assert published(root, "A") == []


def test_sigterm_interrupts_blocked_worker_and_does_not_start_b(runtime):
    _, root = runtime
    database = make_database(root)
    locker = sqlite3.connect(database)
    locker.execute("BEGIN EXCLUSIVE")
    code = """
import importlib.util,pathlib,sys
s=importlib.util.spec_from_file_location('helper',sys.argv[1])
m=importlib.util.module_from_spec(s); s.loader.exec_module(m)
m.APPDATA_ROOT=pathlib.Path(sys.argv[2])
raise SystemExit(m.main(['--instances','A','B','--timeout','20']))
"""
    process = subprocess.Popen(
        [sys.executable, "-B", "-c", code, str(SCRIPT), str(root)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        deadline = time.monotonic() + 5
        partial = root / "backups" / ".partial"
        while not partial.exists() or not list(partial.iterdir()):
            assert time.monotonic() < deadline and process.poll() is None
            time.sleep(.01)
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 130, stderr
        report = json.loads(stdout)
        assert [r["code"] for r in report["results"]] == ["INTERRUPTED", "INTERRUPTED"]
        assert [r["instance"] for r in report["results"]] == ["A", "B"]
        assert published(root, "A") == []
        assert not (root / "B").exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        locker.rollback()
        locker.close()
