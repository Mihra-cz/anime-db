#!/usr/bin/env python3
"""N4B1: wrap one existing Restic backup invocation, not the Nexus service.

No root/config override, restore, purge, retention or service management.
The caller must already hold the existing Nexus backup/maintenance lock and
have verified the NAS and prepared InfluxDB. Only administrator-owned code and
configuration may be installed here. stdout is a single aggregate JSON report.
"""

import hashlib
from datetime import datetime
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
import uuid


class IntegrationError(Exception):
    pass


FALLBACK_APPDATA_ROOT = Path("/srv/nexus/appdata/anime-db")
try:
    _spec = importlib.util.spec_from_file_location("nexus_sqlite_backup", Path(__file__).with_name("sqlite_backup.py"))
    helper = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(helper)
    HelperError = helper.BackupError
    if helper.APPDATA_ROOT != FALLBACK_APPDATA_ROOT:
        raise IntegrationError("Installed N4A root differs from integration contract")
    _helper_import_error = None
except Exception as exc:
    # A broken/missing installation must not take down the existing base backup.
    # The fallback has its own fixed exclusions and attempts no helper operation.
    helper = None
    HelperError = IntegrationError
    _helper_import_error = str(exc)
CONFIG_PATH = Path("/srv/nexus/projects/anime-db/config/sqlite-backup.json")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_FILES = frozenset({"anime.db", "snapshot.json", "SHA256SUMS"})
_LIMIT = 1024 * 1024
_VERIFY_TIMEOUT = 300


def _root():
    return helper.APPDATA_ROOT if helper is not None else FALLBACK_APPDATA_ROOT


def _json(data):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise IntegrationError("Duplicate JSON key")
            value[key] = item
        return value
    try:
        return json.loads(data, object_pairs_hook=unique)
    except (ValueError, UnicodeError) as exc:
        raise IntegrationError("Invalid JSON") from exc


def _configuration():
    if helper is None:
        raise IntegrationError("N4A helper unavailable: " + _helper_import_error)
    # Pin all components. A replaceable symlink cannot supply privileged config.
    with helper._Directories() as paths:
        parent = paths._remember(os.open("/", helper._DIRECTORY))
        for component in CONFIG_PATH.parts[1:-1]:
            parent = paths.child(parent, component)
        helper._protected_directory(parent)
        fd = os.open(CONFIG_PATH.name, helper._READ_FILE, dir_fd=parent)
        with os.fdopen(fd, "rb") as stream:
            helper._regular(stream.fileno(), owner=True)
            config = _json(stream.read(_LIMIT + 1))
        paths.check()
    if not isinstance(config, dict) or set(config) != {"expected_instances"}:
        raise IntegrationError("Configuration requires only expected_instances")
    instances = config["expected_instances"]
    if (not isinstance(instances, list) or not instances or len(instances) > 2
            or any(i not in ("A", "B") for i in instances) or len(set(instances)) != len(instances)):
        raise IntegrationError("Expected instances must explicitly name unique A and/or B")
    return instances


def _command(argv, output, timeout=None):
    # stderr stays in the existing journal; stdout is parsed, never evaluated.
    return subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=output, timeout=timeout, close_fds=True).returncode


def _run_helper(instances):
    with tempfile.TemporaryFile() as output:
        try:
            code = _command(
                [sys.executable, "-I", "-B", str(Path(__file__).with_name("sqlite_backup.py")), "--instances", *instances],
                output, timeout=len(instances) * helper.DEFAULT_TIMEOUT + 30,
            )
        except subprocess.TimeoutExpired:
            return 124, b""
        output.seek(0)
        return code, output.read(_LIMIT + 1)


def _prefix(argv):
    # Deliberately narrow installation contract. Do not reinterpret arbitrary
    # commands/options as read-only verification authority. Preserve the entire
    # original backup suffix verbatim; new excludes only narrow its selection.
    if (len(argv) < 7 or argv[0] != "/usr/bin/restic" or argv[1] != "-r"
            or not argv[2] or argv[3] != "--password-file" or not argv[4] or argv[5] != "backup"):
        raise IntegrationError("Requires /usr/bin/restic -r REPO --password-file PASS backup ORIGINAL_ARGS")
    if "--" in argv[6:] or any(a.split("=", 1)[0] in {
        "--dry-run", "-n", "--skip-if-unchanged", "--stdin", "--stdin-from-command",
        "--files-from", "--files-from-verbatim", "--files-from-raw", "--json",
        "--repo", "-r", "--repository-file", "--password-file", "--password-command",
    } for a in argv[6:]):
        raise IntegrationError("Unsupported backup option; installation contract requires review")
    return argv[:5]


def _excludes():
    root = _root()
    return [str(root / "A/db"), str(root / "B/db"), str(root / "backups/.partial")]


def _report(raw, code, instances):
    report = _json(raw)
    if (not isinstance(report, dict) or type(report.get("format_version")) is not int
            or report["format_version"] != 1 or report.get("expected_instances") != instances
            or report.get("remote_backup_verified") is not False
            or type(report.get("success")) is not bool or not isinstance(report.get("results"), list)
            or len(report["results"]) != len(instances)):
        raise IntegrationError("Helper report contract mismatch")
    for instance, result in zip(instances, report["results"]):
        if (not isinstance(result, dict) or result.get("instance") != instance
                or result.get("status") not in ("SUCCESS", "STOP")
                or result.get("remote_backup_verified") is not False):
            raise IntegrationError("Helper per-instance identity/status mismatch")
        if result["status"] == "SUCCESS":
            generation = result.get("generation")
            if (not isinstance(generation, str) or not helper._GENERATION.fullmatch(generation)
                    or result.get("snapshot") != str(helper.APPDATA_ROOT / "backups" / instance / generation)
                    or result.get("publication_uncertain", False) is not False):
                raise IntegrationError("Invalid successful generation identity")
        elif not isinstance(result.get("code"), str) or not result["code"]:
            raise IntegrationError("STOP requires a machine-readable error")
    success = all(r["status"] == "SUCCESS" for r in report["results"])
    if report["success"] != success or (code == 0) != success or code not in (0, 1, 130):
        raise IntegrationError("Helper exit status disagrees with JSON")
    return report["results"]


def _bundle(result, started_at):
    instance, generation = result["instance"], result["generation"]
    with helper._Directories() as paths:
        root = paths.root()
        backups = paths.child(root, "backups", protected=True)
        directory = paths.child(paths.child(backups, instance, protected=True), generation, protected=True)
        if set(os.listdir(directory)) != _FILES:
            raise IntegrationError("Local generation contains missing/extra files")
        files = {}
        contents = {}
        for name in sorted(_FILES):
            fd = os.open(name, helper._READ_FILE, dir_fd=directory)
            with os.fdopen(fd, "rb") as stream:
                info = helper._regular(stream.fileno(), owner=True)
                if helper._mount_id(stream.fileno()) != paths.mount_id:
                    raise IntegrationError("Local generation file is a mount alias")
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
                files[name] = {"sha256": digest, "size": info.st_size}
                if name != "anime.db":
                    if info.st_size > _LIMIT:
                        raise IntegrationError("Oversized snapshot metadata")
                    stream.seek(0)
                    contents[name] = stream.read(_LIMIT + 1)
        metadata = _json(contents["snapshot.json"])
        expected_sums = "".join(f"{files[n]['sha256']}  {n}\n" for n in ("anime.db", "snapshot.json")).encode()
        if contents["SHA256SUMS"] != expected_sums:
            raise IntegrationError("Local SHA256SUMS mismatch")
        if (not isinstance(metadata, dict) or metadata.get("format_version") != 1
                or metadata.get("instance") != instance or metadata.get("generation") != generation
                or metadata.get("source", {}).get("path") != str(helper.APPDATA_ROOT / instance / "db/anime.db")
                or metadata.get("schema_version") != helper.EXPECTED_SCHEMA_VERSION
                or metadata.get("remote_backup_verified") is not False
                or metadata.get("database_sha256") != files["anime.db"]["sha256"]
                or metadata.get("database_size") != files["anime.db"]["size"]):
            raise IntegrationError("Local manifest identity/hash/schema mismatch")
        validation = metadata.get("validation", {})
        if (validation.get("integrity_check") != "ok" or validation.get("foreign_key_check") != []
                or validation.get("standalone") is not True
                or not helper.EXPECTED_TABLES.issubset(validation.get("tables", []))):
            raise IntegrationError("Local manifest does not prove N4A validation")
        try:
            started = datetime.fromisoformat(metadata["started_at"])
            completed = datetime.fromisoformat(metadata["completed_at"])
            if not (datetime.fromisoformat(started_at) <= started <= completed <= datetime.fromisoformat(helper._utc_now())):
                raise ValueError("Snapshot is outside the current helper invocation")
        except (KeyError, ValueError, TypeError) as exc:
            raise IntegrationError("Local generation is not from this run") from exc
        paths.check()
    return {"instance": instance, "generation": generation, "path": result["snapshot"], "files": files}


def _lines(stream):
    stream.seek(0)
    while True:
        line = stream.readline(_LIMIT + 1)
        if not line:
            return
        if len(line) > _LIMIT:
            raise IntegrationError("Oversized JSON line")
        value = _json(line)
        if not isinstance(value, dict):
            raise IntegrationError("Expected JSON object")
        yield value


def _snapshot_id(output):
    summaries = []
    error = False
    last = None
    for row in _lines(output):
        last = row
        error |= row.get("message_type") in ("error", "exit_error")
        if row.get("message_type") == "summary":
            summaries.append(row)
    if (error or len(summaries) != 1 or last != summaries[0]
            or summaries[0].get("dry_run", False) is not False
            or not isinstance(summaries[0].get("snapshot_id"), str)
            or not _HASH.fullmatch(summaries[0]["snapshot_id"])):
        raise IntegrationError("Backup lacks a unique final successful full snapshot ID")
    return summaries[0]["snapshot_id"]


def _read_command(prefix, args, output):
    code = _command([*prefix, *args], output, timeout=_VERIFY_TIMEOUT)
    if code != 0:
        raise IntegrationError(f"Restic {args[0]} failed with exit {code}")
    output.seek(0)


def _verify_remote(prefix, snapshot_id, bundles, forbidden):
    with tempfile.TemporaryFile() as output:
        _read_command(prefix, ["cat", "config"], output)
        config = _json(output.read(_LIMIT + 1))
        if not isinstance(config, dict) or not isinstance(config.get("id"), str) or not _HASH.fullmatch(config["id"]):
            raise IntegrationError("Cannot establish repository identity")
        repository_id = config["id"]
    expected = {b["path"] + "/" + name: data for b in bundles for name, data in b["files"].items()}
    seen, snapshots = set(), []
    with tempfile.TemporaryFile() as output:
        _read_command(prefix, ["ls", "--json", snapshot_id], output)
        for row in _lines(output):
            kind = row.get("struct_type", row.get("message_type"))
            if kind == "snapshot":
                snapshots.append(row.get("id"))
                continue
            if kind != "node":
                raise IntegrationError("Unrecognized Restic ls record")
            path = row.get("path")
            if not isinstance(path, str) or not path.startswith("/") or str(PurePosixPath(path)) != path or ".." in PurePosixPath(path).parts:
                raise IntegrationError("Noncanonical Restic node path")
            if any(path == f or path.startswith(f + "/") for f in forbidden):
                raise IntegrationError("Snapshot contains excluded/uncertain data")
            for bundle in bundles:
                if path.startswith(bundle["path"] + "/"):
                    if path not in expected or path in seen or row.get("type") != "file" or row.get("size", 0) != expected[path]["size"]:
                        raise IntegrationError("Remote generation has extra, duplicate or invalid files")
                    seen.add(path)
        if snapshots != [snapshot_id] or seen != set(expected):
            raise IntegrationError("Wrong snapshot identity or missing current generation")
    for path, data in expected.items():
        with tempfile.TemporaryFile() as output:
            _read_command(prefix, ["dump", snapshot_id, path], output)
            # Hash bytes returned from the repository, never trust only ls sizes
            # or Restic's successful backup exit status. No restore target exists.
            digest = hashlib.file_digest(output, "sha256").hexdigest()
            if output.tell() != data["size"] or digest != data["sha256"]:
                raise IntegrationError("Remote generation content checksum mismatch")
    return repository_id


def _receipt(snapshot_id, repository_id, bundles):
    record = {"format_version": 1, "kind": "restic-content-verification", "verified_at": helper._utc_now(),
              "snapshot_id": snapshot_id, "repository_id": repository_id, "generations": bundles,
              "restore_tested": False}
    with helper._Directories() as paths:
        root = paths.root()
        backups = paths.child(root, "backups", protected=True)
        directory = paths.child(backups, ".restic-confirmations", create=True, protected=True)
        name = uuid.uuid4().hex + ".json"
        temporary = ".partial-" + name
        helper._write_file(directory, temporary, (json.dumps(record, indent=2, sort_keys=True) + "\n").encode())
        paths.check()
        helper._rename_noreplace(directory, temporary, directory, name)
        os.fsync(directory)
    return str(helper.APPDATA_ROOT / "backups/.restic-confirmations" / name)


def run_backup(argv):
    prefix = _prefix(argv)
    report = {"format_version": 1, "success": False, "instances": [], "helper_exit_code": None,
              "helper_error": None, "restic_exit_code": None, "snapshot_id": None,
              "repository_id": None, "verification_error": None, "receipt": None, "restore_tested": False}
    excludes, bundles = _excludes(), []
    instances = []
    try:
        instances = _configuration()
        started_at = helper._utc_now()
        code, raw = _run_helper(instances)
        report["helper_exit_code"] = code
        results = _report(raw, code, instances)
        for result in results:
            item = dict(result)
            report["instances"].append(item)
            if result["status"] == "SUCCESS":
                try:
                    bundles.append(_bundle(result, started_at))
                except (IntegrationError, OSError, HelperError, TypeError, AttributeError) as exc:
                    item.update(status="STOP", code="LOCAL_BUNDLE", detail=str(exc))
                    excludes.append(str(helper.APPDATA_ROOT / "backups" / result["instance"]))
            elif result.get("publication_uncertain"):
                excludes.append(str(helper.APPDATA_ROOT / "backups" / result["instance"]))
    except (IntegrationError, OSError, HelperError, subprocess.SubprocessError, TypeError, AttributeError) as exc:
        report["helper_error"] = str(exc)
        report["instances"] = [{"instance": i, "status": "STOP", "code": "HELPER_REPORT", "remote_backup_verified": False} for i in instances]
        bundles = []
        excludes.extend(str(_root() / "backups" / i) for i in ("A", "B"))
    # Keep the old argv including sources, excludes, tags and repo. Only append
    # JSON output and explicit exclusions, even when AnimeDB preparation failed.
    command = [*argv, "--json"]
    for exclude in excludes:
        command.extend(["--exclude", exclude])
    try:
        output = tempfile.TemporaryFile()
    except OSError as exc:
        # Lack of local capture space does not justify skipping Nexus/InfluxDB.
        # Without output we cannot bind a snapshot ID, so no confirmation occurs.
        report["verification_error"] = "Cannot capture Restic output: " + str(exc)
        with open(os.devnull, "wb") as sink:
            try:
                code = _command(command, sink)
                report["restic_exit_code"] = code
                return report, (code if code > 0 else 128 - code) if code else 1
            except (OSError, subprocess.SubprocessError) as command_error:
                report["verification_error"] += "; " + str(command_error)
                return report, 1
    with output:
        try:
            restic_code = _command(command, output)
            report["restic_exit_code"] = restic_code
            if restic_code != 0:
                return report, restic_code if restic_code > 0 else 128 - restic_code
            report["snapshot_id"] = _snapshot_id(output)
            report["repository_id"] = _verify_remote(prefix, report["snapshot_id"], bundles, excludes)
            if bundles:
                report["receipt"] = _receipt(report["snapshot_id"], report["repository_id"], bundles)
                for item in report["instances"]:
                    if item["status"] == "SUCCESS":
                        item["remote_backup_verified"] = True
        except (IntegrationError, OSError, HelperError, subprocess.SubprocessError, TypeError, AttributeError) as exc:
            report["verification_error"] = str(exc)
    report["success"] = bool(report["instances"]) and not report["helper_error"] and not report["verification_error"] and all(
        r["remote_backup_verified"] for r in report["instances"]
    )
    return report, 0 if report["success"] else 1


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    try:
        if not args or args[0] != "--":
            raise IntegrationError("Usage: sqlite_restic.py -- /usr/bin/restic -r REPO --password-file PASS backup ORIGINAL_ARGS")
        report, code = run_backup(args[1:])
    except IntegrationError as exc:
        report, code = {"success": False, "configuration_error": str(exc), "restore_tested": False}, 2
    print(json.dumps(report, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
