"""N4B1 contract: real temporary SQLite snapshots, simulated Restic only."""

from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest
from sqlalchemy import create_engine

from app.database import Base
from app import models  # Register schema without application startup.


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "deploy/nexus/scripts/sqlite_restic.py"
SNAPSHOT = "a1" * 32
REPOSITORY = "b2" * 32
COMMAND = [
    "/usr/bin/restic", "-r", "/mnt/nas-backup/restic-nexus",
    "--password-file", "/etc/restic/nexus.password", "backup",
    "/etc", "/srv/nexus", "/usr/local/sbin", "/var/lib/nexus-backup-staging/influxdb",
    "--exclude", "/etc/restic/nexus.password", "--tag", "nexus-daily",
]


def load_adapter():
    assert SCRIPT.is_file(), "N4B1 Restic invocation adapter is missing"
    spec = importlib.util.spec_from_file_location("nexus_sqlite_restic", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeRestic:
    """Literal absolute excludes, immutable file contents and JSON from Restic 0.18.

    No Restic executable, repository, backup or restore is used by these tests.
    Directory pruning models Restic's documented anchored literal patterns.
    """

    def __init__(self, root):
        self.root = root
        self.calls = []
        self.files = {}
        self.exit_code = 0
        self.fault = None
        self.excludes = []

    def __call__(self, argv, output, timeout=None):
        self.calls.append(list(argv))
        assert argv[:5] == COMMAND[:5]
        verb = argv[5]
        if verb == "backup":
            self.excludes = [argv[i + 1] for i, item in enumerate(argv) if item == "--exclude"]
            assert "--json" in argv
            assert argv[6:10] == COMMAND[6:10]  # Preserve original source groups/retention grouping.
            assert argv[argv.index("--tag") + 1] == "nexus-daily"
            paths = {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
            paths.update({
                "/etc/restic/nexus.password": b"secret",
                "/etc/system.conf": b"system", "/usr/local/sbin/nexus-influxdb-stage": b"script",
                "/var/lib/nexus-backup-staging/influxdb/current/manifest": b"influx",
                "/srv/nexus/projects/anime-db/manifest.toml": b"planned",
                "/srv/nexus/projects/anime-db/docs/OPERATIONS.md": b"docs",
            })
            # These tests intentionally use no glob interpretation: only absolute,
            # metacharacter-free directory/file paths are supported by this fake.
            assert all(p.startswith("/") and not any(c in p for c in "*?[\\") for p in self.excludes)
            self.files = {p: data for p, data in paths.items() if not any(
                p == exclude or p.startswith(exclude + "/") for exclude in self.excludes
            )}
            if self.fault == "missing-generation":
                self.files = {p: b for p, b in self.files.items() if not p.endswith("/SHA256SUMS")}
            elif self.fault in ("checksum", "manifest", "sums"):
                suffix = {"checksum": "/anime.db", "manifest": "/snapshot.json", "sums": "/SHA256SUMS"}[self.fault]
                path = next(p for p in self.files if "/backups/A/" in p and p.endswith(suffix))
                original = self.files[path]
                self.files[path] = bytes([original[0] ^ 1]) + original[1:]
            elif self.fault == "live-db":
                self.files[str(self.root / "A/db/anime.db")] = b"live"
            elif self.fault == "partial":
                self.files[str(self.root / "backups/.partial/unpublished/anime.db")] = b"partial"
            elif self.fault == "extra-sidecar":
                path = next(p for p in self.files if "/backups/A/" in p and p.endswith("/anime.db"))
                self.files[path + "-wal"] = b"wal"
            summary = {"message_type": "summary", "snapshot_id": SNAPSHOT}
            if self.fault == "missing-id":
                del summary["snapshot_id"]
            elif self.fault == "short-id":
                summary["snapshot_id"] = SNAPSHOT[:8]
            output.write((json.dumps(summary) + "\n").encode())
            return self.exit_code
        if verb == "cat":
            assert argv[6:] == ["config"]
            config = {
                "repository-empty": {},
                "repository-invalid-id": {"id": "zz"},
                "repository-id-type": {"id": 123},
                "repository-not-object": [],
            }.get(self.fault, {"version": 2, "id": REPOSITORY})
            output.write(json.dumps(config).encode())
        elif verb == "ls":
            assert argv[6:] == ["--json", SNAPSHOT]
            identity = "c3" * 32 if self.fault == "wrong-id" else SNAPSHOT
            rows = [{"struct_type": "snapshot", "id": identity}]
            directories = set()
            for path in self.files:
                directories.update(str(p) for p in Path(path).parents if str(p) != "/")
            rows += [{"struct_type": "node", "type": "dir", "path": p} for p in sorted(directories)]
            rows += [{"struct_type": "node", "type": "file", "path": p, "size": len(b)} for p, b in self.files.items()]
            if self.fault == "ls-json":
                output.write(b"not JSON\n")
            else:
                output.write("".join(json.dumps(row) + "\n" for row in rows).encode())
        elif verb == "dump":
            assert argv[6] == SNAPSHOT
            output.write(self.files[argv[7]])
            if self.fault == "dump-failure":
                return 1
        else:
            pytest.fail(f"Unexpected/destructive Restic command: {verb}")
        return 0


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    adapter = load_adapter()
    root = tmp_path / "appdata"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(adapter.helper, "APPDATA_ROOT", root)
    config_dir = tmp_path / "config"
    config_dir.mkdir(mode=0o700)
    config = config_dir / "sqlite-backup.json"
    config.write_text('{"expected_instances": ["A"]}\n')
    config.chmod(0o600)
    monkeypatch.setattr(adapter, "CONFIG_PATH", config)
    fake = FakeRestic(root)
    monkeypatch.setattr(adapter, "_command", fake)

    def actual_helper(instances):
        report = adapter.helper.backup_instances(instances)
        return (0 if report["success"] else 1), json.dumps(report).encode()

    monkeypatch.setattr(adapter, "_run_helper", actual_helper)
    return adapter, root, config, fake


def database(root, instance="A"):
    path = root / instance / "db/anime.db"
    path.parent.mkdir(parents=True, mode=0o700)
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA user_version=9")
        connection.execute(models.CatalogCollection.__table__.insert().values(
            id=1, local_title=instance, normalized_local_title=instance.lower(), relative_root_path=instance,
        ))
    engine.dispose()
    return path


def execute(runtime):
    return runtime[0].run_backup(COMMAND)


@pytest.mark.parametrize("instances", [["A"], ["A", "B"]])
def test_exact_current_generations_are_remotely_verified_and_receipted(runtime, instances):
    adapter, root, config, fake = runtime
    config.write_text(json.dumps({"expected_instances": instances}))
    originals = {i: database(root, i).read_bytes() for i in instances}
    artwork = root / "A/artwork/cache.jpg"
    artwork.parent.mkdir()
    artwork.write_bytes(b"artwork")
    reference = root / "reference/old/anime.db"
    reference.parent.mkdir(parents=True)
    reference.write_bytes(b"reference")
    partial = root / "backups/.partial/interrupted/anime.db"
    (root / "backups").mkdir(mode=0o700)
    (root / "backups/.partial").mkdir(mode=0o700)
    partial.parent.mkdir(mode=0o700)
    partial.write_bytes(b"incomplete")
    report, status = execute(runtime)
    assert status == 0 and report["success"] is True, report
    assert report["snapshot_id"] == SNAPSHOT
    assert report["repository_id"] == REPOSITORY
    assert report["restore_tested"] is False
    assert [r["instance"] for r in report["instances"]] == instances
    assert all(r["remote_backup_verified"] is True for r in report["instances"])
    receipt = json.loads(Path(report["receipt"]).read_text())
    assert receipt["snapshot_id"] == SNAPSHOT and receipt["repository_id"] == REPOSITORY
    assert receipt["restore_tested"] is False
    assert len(receipt["generations"]) == len(instances)
    for item in receipt["generations"]:
        bundle = Path(item["path"])
        assert item["instance"] in instances
        assert item["generation"] == bundle.name
        assert set(item["files"]) == {"anime.db", "snapshot.json", "SHA256SUMS"}
        for name, data in item["files"].items():
            assert data["sha256"] == sha256((bundle / name).read_bytes()).hexdigest()
        assert json.loads((bundle / "snapshot.json").read_text())["remote_backup_verified"] is False
    assert str(artwork) in fake.files and str(reference) in fake.files
    assert "/etc/system.conf" in fake.files
    assert "/usr/local/sbin/nexus-influxdb-stage" in fake.files
    assert "/var/lib/nexus-backup-staging/influxdb/current/manifest" in fake.files
    assert "/srv/nexus/projects/anime-db/manifest.toml" in fake.files
    assert "/srv/nexus/projects/anime-db/docs/OPERATIONS.md" in fake.files
    assert "/etc/restic/nexus.password" not in fake.files
    assert str(partial) not in fake.files
    for instance in instances:
        assert str(root / instance / "db/anime.db") not in fake.files
        assert (root / instance / "db/anime.db").read_bytes() == originals[instance]
    if instances == ["A"]:
        assert not (root / "B").exists()
        assert not (root / "backups/B").exists()


def test_missing_active_b_keeps_system_influx_and_a_but_fails_overall(runtime):
    _, root, config, fake = runtime
    config.write_text('{"expected_instances":["A","B"]}')
    database(root)
    report, status = execute(runtime)
    assert status == 1 and report["success"] is False
    assert report["instances"][0]["remote_backup_verified"] is True
    assert report["instances"][1]["remote_backup_verified"] is False
    assert report["instances"][1]["code"] == "MISSING_SOURCE"
    assert "/etc/system.conf" in fake.files
    assert "/var/lib/nexus-backup-staging/influxdb/current/manifest" in fake.files
    assert not (root / "B").exists()


@pytest.mark.parametrize("raw,rc", [(b"oops", 1), (b"[]", 0), (b"{}", 0), (b"", 124)])
def test_untrusted_helper_report_continues_base_backup_without_false_confirmation(runtime, monkeypatch, raw, rc):
    adapter, root, _, fake = runtime
    database(root)
    old = root / "backups/A/old/anime.db"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"preserve")
    monkeypatch.setattr(adapter, "_run_helper", lambda _: (rc, raw))
    report, status = execute(runtime)
    assert status == 1 and report["success"] is False and report["receipt"] is None
    assert report["helper_error"]
    assert all(not r["remote_backup_verified"] for r in report["instances"])
    assert str(old) not in fake.files and old.read_bytes() == b"preserve"
    assert "/etc/system.conf" in fake.files
    assert "/var/lib/nexus-backup-staging/influxdb/current/manifest" in fake.files


@pytest.mark.parametrize("fault", ["missing-id", "short-id", "wrong-id", "missing-generation", "checksum", "manifest", "sums", "live-db", "partial", "extra-sidecar", "ls-json", "dump-failure"])
def test_incomplete_remote_proof_never_confirms_or_deletes_local_generation(runtime, fault):
    _, root, _, fake = runtime
    database(root)
    fake.fault = fault
    report, status = execute(runtime)
    assert status == 1 and report["success"] is False
    assert report["verification_error"] and report["receipt"] is None
    assert all(not r["remote_backup_verified"] for r in report["instances"])
    assert len(list((root / "backups/A").iterdir())) == 1
    assert not list((root / "backups").glob(".restic-confirmations/*.json"))


@pytest.mark.parametrize("exit_code", [1, 3, 10, 130])
def test_restic_failure_including_three_preserves_exit_status_and_unconfirmed_files(runtime, exit_code):
    _, root, _, fake = runtime
    database(root)
    fake.exit_code = exit_code
    report, status = execute(runtime)
    assert status == exit_code and report["restic_exit_code"] == exit_code
    assert report["receipt"] is None and report["success"] is False
    assert not report["instances"][0]["remote_backup_verified"]
    assert len(list((root / "backups/A").iterdir())) == 1
    assert [argv[5] for argv in fake.calls] == ["backup"]


@pytest.mark.parametrize("configuration", ['{"expected_instances":[]}', '{"expected_instances":["A","A"]}', '{"expected_instances":["C"]}', 'garbage'])
def test_bad_explicit_configuration_fails_anime_but_preserves_base_backup(runtime, configuration):
    _, _, config, fake = runtime
    config.write_text(configuration)
    report, status = execute(runtime)
    assert status == 1 and report["helper_error"]
    assert report["receipt"] is None
    assert "/etc/system.conf" in fake.files


def test_repeated_runs_do_not_replace_or_purge_generations(runtime):
    _, root, _, _ = runtime
    database(root)
    paths = []
    for _ in range(3):
        report, status = execute(runtime)
        assert status == 0
        paths.append(Path(report["receipt"]))
    assert len(set(paths)) == 3
    assert len(list((root / "backups/A").iterdir())) == 3
    assert all(p.exists() for p in paths)


def test_original_service_shell_keeps_lock_and_failure_status_for_onfailure(tmp_path):
    """Exercise the shell contract without systemd, services, NAS or Restic."""
    lock = tmp_path / "backup.lock"
    log = tmp_path / "log"
    adapter = tmp_path / "adapter.py"
    adapter.write_text(
        "import subprocess, sys\n"
        "assert subprocess.run(['flock', '-n', sys.argv[2], 'true']).returncode != 0\n"
        "sys.exit(int(sys.argv[1]))\n"
    )
    for status in (0, 1, 3):
        command = f'''set -Eeuo pipefail
exec 9>"$1"
flock -n 9
printf 'influx prepared\\n' > "$2"
{sys.executable} "$3" "$4" "$1"
printf 'PASS\\n' >> "$2"
'''
        result = subprocess.run(["bash", "-c", command, "test", str(lock), str(log), str(adapter), str(status)])
        assert result.returncode == status
        assert log.read_text() == ("influx prepared\nPASS\n" if status == 0 else "influx prepared\n")


def test_previous_generation_cannot_be_replayed_as_current_success(runtime, monkeypatch):
    adapter, root, _, fake = runtime
    database(root)
    old = adapter.helper.backup_instances(["A"])
    monkeypatch.setattr(adapter, "_run_helper", lambda _: (0, json.dumps(old).encode()))
    report, status = execute(runtime)
    assert status == 1 and report["receipt"] is None
    assert report["instances"][0]["code"] == "LOCAL_BUNDLE"
    assert not report["instances"][0]["remote_backup_verified"]
    assert "/etc/system.conf" in fake.files


@pytest.mark.parametrize("fault", ["exit-mismatch", "duplicate-key", "wrong-instance", "traversal", "uncertain-success"])
def test_inconsistent_helper_json_is_rejected_before_confirmation(runtime, monkeypatch, fault):
    adapter, root, _, fake = runtime
    database(root)
    actual = adapter._run_helper

    def broken(instances):
        code, raw = actual(instances)
        value = json.loads(raw)
        if fault == "exit-mismatch":
            return 1, raw
        if fault == "duplicate-key":
            return code, raw[:-1] + b',"success":true}'
        if fault == "wrong-instance":
            value["results"][0]["instance"] = "B"
        elif fault == "traversal":
            value["results"][0]["snapshot"] += "/../../B"
        else:
            value["results"][0]["publication_uncertain"] = True
        return code, json.dumps(value).encode()

    monkeypatch.setattr(adapter, "_run_helper", broken)
    report, status = execute(runtime)
    assert status == 1 and report["helper_error"] and report["receipt"] is None
    assert "/etc/system.conf" in fake.files
    assert not any("/backups/A/" in p for p in fake.files)


def test_publication_uncertainty_excludes_affected_tree_without_dropping_system_backup(runtime, monkeypatch):
    adapter, root, _, fake = runtime
    invalid = root / "backups/A/uncertain/anime.db"
    invalid.parent.mkdir(parents=True)
    invalid.write_bytes(b"untrusted")
    raw = {"format_version": 1, "expected_instances": ["A"], "success": False,
           "remote_backup_verified": False, "results": [{"instance": "A", "status": "STOP",
           "code": "IO_ERROR", "remote_backup_verified": False, "publication_uncertain": True}]}
    monkeypatch.setattr(adapter, "_run_helper", lambda _: (1, json.dumps(raw).encode()))
    report, status = execute(runtime)
    assert status == 1 and report["receipt"] is None and str(invalid) not in fake.files
    assert invalid.read_bytes() == b"untrusted"
    assert "/var/lib/nexus-backup-staging/influxdb/current/manifest" in fake.files


@pytest.mark.parametrize("fault", ["config-mode", "config-symlink", "config-hardlink", "missing-config"])
def test_unsafe_admin_config_never_activates_instances(runtime, fault):
    _, _, config, fake = runtime
    if fault == "config-mode":
        config.chmod(0o666)
    elif fault == "missing-config":
        config.unlink()
    else:
        other = config.with_name("other")
        config.rename(other)
        if fault == "config-symlink":
            config.symlink_to(other)
        else:
            os.link(other, config)
    report, status = execute(runtime)
    assert status == 1 and report["helper_error"] and report["receipt"] is None
    assert "/etc/system.conf" in fake.files


def test_receipt_failure_does_not_confirm_or_purge(runtime, monkeypatch):
    adapter, root, _, _ = runtime
    database(root)
    def fail_receipt(*_):
        raise OSError("injected fsync/write error")
    monkeypatch.setattr(adapter, "_receipt", fail_receipt)
    report, status = execute(runtime)
    assert status == 1 and report["verification_error"]
    assert not report["instances"][0]["remote_backup_verified"]
    assert len(list((root / "backups/A").iterdir())) == 1


@pytest.mark.parametrize("suffix", [["--dry-run"], ["--skip-if-unchanged"], ["--", "/srv/nexus"], ["--repo", "/other"]])
def test_unsupported_restic_interface_stops_before_running_any_command(runtime, suffix):
    adapter, _, _, fake = runtime
    with pytest.raises(adapter.IntegrationError):
        adapter.run_backup([*COMMAND, *suffix])
    assert fake.calls == []


def test_live_directories_are_pruned_with_other_db_names_and_inactive_b_present(runtime):
    _, root, _, fake = runtime
    database(root)
    (root / "A/db/other.db").write_bytes(b"another live DB")
    (root / "B/db").mkdir(parents=True)
    inactive = root / "B/db/anime.db"
    inactive.write_bytes(b"not initialized; must not be opened by helper")
    report, status = execute(runtime)
    assert status == 0 and [r["instance"] for r in report["instances"]] == ["A"]
    assert not any(p.startswith(str(root / "A/db") + "/") for p in fake.files)
    assert not any(p.startswith(str(root / "B/db") + "/") for p in fake.files)
    assert inactive.read_bytes() == b"not initialized; must not be opened by helper"
    assert not (root / "backups/B").exists()


def test_missing_helper_module_still_runs_safe_base_backup(tmp_path, monkeypatch):
    # Copy only the adapter, deliberately omit its adjacent installed helper.
    copied = tmp_path / "sqlite_restic.py"
    copied.write_bytes(SCRIPT.read_bytes())
    spec = importlib.util.spec_from_file_location("nexus_missing_helper", copied)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except OSError as exc:
        pytest.fail(f"Missing helper aborts before base backup: {exc}")
    root = tmp_path / "appdata"
    root.mkdir()
    monkeypatch.setattr(module, "FALLBACK_APPDATA_ROOT", root)
    fake = FakeRestic(root)
    monkeypatch.setattr(module, "_command", fake)
    report, status = module.run_backup(COMMAND)
    assert status == 1 and report["helper_error"] and report["receipt"] is None
    assert "/etc/system.conf" in fake.files
    assert "/var/lib/nexus-backup-staging/influxdb/current/manifest" in fake.files
    assert str(root / "backups/A") in fake.excludes
    assert str(root / "backups/B") in fake.excludes


@pytest.mark.parametrize("restic_status", [0, 3])
def test_spool_allocation_failure_preserves_base_backup_without_confirmation(runtime, monkeypatch, restic_status):
    adapter, root, _, fake = runtime
    database(root)
    fake.exit_code = restic_status
    def no_space(*_, **__):
        raise OSError("No space left on temporary filesystem")
    monkeypatch.setattr(adapter.tempfile, "TemporaryFile", no_space)
    report, status = execute(runtime)
    assert status == (restic_status or 1) and report["verification_error"] and report["receipt"] is None
    assert report["restic_exit_code"] == restic_status
    assert "/etc/system.conf" in fake.files
    assert "/var/lib/nexus-backup-staging/influxdb/current/manifest" in fake.files
    assert len(list((root / "backups/A").iterdir())) == 1


@pytest.mark.parametrize("instances", [["A"], ["A", "B"]])
def test_helper_subprocess_protocol_with_real_isolated_cli(tmp_path, monkeypatch, instances):
    adapter = load_adapter()
    root = tmp_path / "appdata"
    root.mkdir(mode=0o700)
    for instance in instances:
        database(root, instance)
    monkeypatch.setattr(adapter.helper, "APPDATA_ROOT", root)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"expected_instances": instances}))
    config.chmod(0o600)
    tmp_path.chmod(0o700)
    monkeypatch.setattr(adapter, "CONFIG_PATH", config)
    command = adapter._command
    fake = FakeRestic(root)
    helper_script = SCRIPT.with_name("sqlite_backup.py")
    # Test-only subprocess launcher pins the temporary root before calling the
    # real helper CLI. Production CLI still has no root/environment override.
    launcher = (
        "import importlib.util,pathlib,sys; "
        "s=importlib.util.spec_from_file_location('isolated_helper',sys.argv[1]); "
        "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
        "m.APPDATA_ROOT=pathlib.Path(sys.argv[2]); sys.exit(m.main(sys.argv[3:]))"
    )
    def boundary(argv, output, timeout=None):
        if argv[0] == "/usr/bin/restic":
            return fake(argv, output, timeout)
        assert argv == [sys.executable, "-I", "-B", str(helper_script), "--instances", *instances]
        assert timeout == 120 * len(instances) + 30
        return command([sys.executable, "-I", "-B", "-c", launcher, str(helper_script), str(root), "--instances", *instances], output, timeout)
    monkeypatch.setattr(adapter, "_command", boundary)
    report, status = adapter.run_backup(COMMAND)
    assert status == 0 and all(r["remote_backup_verified"] for r in report["instances"]), report


def test_main_emits_single_json_and_preserves_exit_three(runtime, capsys):
    adapter, root, _, fake = runtime
    database(root)
    fake.exit_code = 3
    assert adapter.main(["--", *COMMAND]) == 3
    result = json.loads(capsys.readouterr().out)
    assert result["restic_exit_code"] == 3 and result["success"] is False


@pytest.mark.parametrize("fault", [
    "repository-empty", "repository-invalid-id", "repository-id-type", "repository-not-object",
])
def test_invalid_repository_identity_is_a_controlled_verification_failure(runtime, fault):
    _, root, _, fake = runtime
    database(root)
    fake.fault = fault
    report, status = execute(runtime)
    assert status == 1 and report["success"] is False
    assert report["helper_error"] is None and report["helper_exit_code"] == 0
    assert report["restic_exit_code"] == 0 and report["snapshot_id"] == SNAPSHOT
    assert report["verification_error"] == "Cannot establish repository identity"
    assert report["repository_id"] is None and report["receipt"] is None
    assert report["instances"][0]["status"] == "SUCCESS"  # Local copy alone is insufficient.
    assert report["instances"][0]["remote_backup_verified"] is False
    assert not list((root / "backups").glob(".restic-confirmations/*.json"))


def test_local_checksum_mismatch_is_rejected_before_remote_confirmation(runtime, monkeypatch):
    adapter, root, _, fake = runtime
    database(root)
    actual = adapter._run_helper
    injected = []

    def corrupt_local_sums(instances):
        code, raw = actual(instances)
        result = json.loads(raw)
        assert code == 0 and result["success"] is True
        bundle = Path(result["results"][0]["snapshot"])
        sums = bundle / "SHA256SUMS"
        original = sums.read_bytes()
        expected = "".join(
            f"{sha256((bundle / name).read_bytes()).hexdigest()}  {name}\n"
            for name in ("anime.db", "snapshot.json")
        ).encode()
        assert original == expected  # N4A really published a valid local bundle.
        damaged = b"0" * 64 + b"  anime.db\n" + b"0" * 64 + b"  snapshot.json\n"
        sums.write_bytes(damaged)
        assert sums.read_bytes() == damaged and damaged != expected
        injected.append(sums)
        return code, raw

    monkeypatch.setattr(adapter, "_run_helper", corrupt_local_sums)
    report, status = execute(runtime)
    assert len(injected) == 1, "Local checksum corruption was not performed"
    assert status == 1 and report["success"] is False
    assert report["helper_error"] is None and report["helper_exit_code"] == 0
    assert report["verification_error"] is None and report["restic_exit_code"] == 0
    result = report["instances"][0]
    assert result["status"] == "STOP" and result["code"] == "LOCAL_BUNDLE"
    assert result["detail"] == "Local SHA256SUMS mismatch"
    assert result["remote_backup_verified"] is False and report["receipt"] is None
    assert str(injected[0]) not in fake.files
    assert "/etc/system.conf" in fake.files
    assert not list((root / "backups").glob(".restic-confirmations/*.json"))


@pytest.mark.parametrize("mode", [0o640, 0o604], ids=["group-readable", "other-readable"])
def test_nonprivate_local_database_reaches_permission_check_and_is_rejected(runtime, monkeypatch, mode):
    adapter, root, _, fake = runtime
    database(root)
    actual = adapter._run_helper
    regular = adapter.helper._regular
    injected, checked = [], []

    def expose_local_database(instances):
        code, raw = actual(instances)
        result = json.loads(raw)
        assert code == 0 and result["success"] is True
        target = Path(result["results"][0]["snapshot"]) / "anime.db"
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        target.chmod(mode)
        identity = target.stat()
        assert stat.S_IMODE(identity.st_mode) == mode
        injected.append(target)

        def observe_regular(fd, **kwargs):
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) == (identity.st_dev, identity.st_ino):
                checked.append(stat.S_IMODE(info.st_mode))
            return regular(fd, **kwargs)  # Keep the real permission validator.

        monkeypatch.setattr(adapter.helper, "_regular", observe_regular)
        return code, raw

    monkeypatch.setattr(adapter, "_run_helper", expose_local_database)
    report, status = execute(runtime)
    assert len(injected) == 1, "Permission injection was not performed"
    assert checked == [mode], "Modified DB did not reach the regular-file permission validator"
    assert status == 1 and report["success"] is False
    assert report["helper_error"] is None and report["helper_exit_code"] == 0
    assert report["verification_error"] is None and report["restic_exit_code"] == 0
    result = report["instances"][0]
    assert result["status"] == "STOP" and result["code"] == "LOCAL_BUNDLE"
    assert result["detail"] == "Backup files must be private to the service account"
    assert result["remote_backup_verified"] is False and report["receipt"] is None
    assert str(injected[0]) not in fake.files
    assert "/etc/system.conf" in fake.files
    assert not list((root / "backups").glob(".restic-confirmations/*.json"))


def test_unknown_configuration_key_is_rejected_loudly(runtime):
    _, root, config, fake = runtime
    database(root)
    config.write_text(json.dumps({"expected_instances": ["A"], "excludes": []}))
    report, status = execute(runtime)
    assert status == 1 and report["success"] is False
    assert report["helper_error"] == "Configuration requires only expected_instances"
    assert report["helper_exit_code"] is None and report["instances"] == []
    assert report["verification_error"] is None and report["restic_exit_code"] == 0
    assert report["receipt"] is None and not (root / "backups").exists()
    assert "/etc/system.conf" in fake.files
