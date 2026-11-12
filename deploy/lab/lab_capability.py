#!/usr/bin/env python3
"""LAB ONLY: capability proof for the current lab RW CIFS mount.

Runs disposable checks in a fresh work directory on the RW view (create, fsync,
RENAME_NOREPLACE rejection, cross-directory rename identity), verifies that the
RO view is a CIFS mount of the same lab share that refuses writes, removes all
of its own artifacts and then writes the root-owned mount-bound runtime evidence
the real coordinator expects. Uses the coordinator's own host_mount_identity.
A remount changes that identity, so the proof must be regenerated.
"""
import argparse
import ctypes
import errno
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import sys

LIBEXEC = Path('/srv/animedb-v6-lab/libexec')


def coordinator():
    sys.path.insert(0, str(LIBEXEC))
    spec = importlib.util.spec_from_file_location('reorganization_coordinator', LIBEXEC / 'reorganization_coordinator.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def rename_noreplace(source, target):
    call = ctypes.CDLL(None, use_errno=True).renameat2
    call.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    if call(-100, os.fsencode(source), -100, os.fsencode(target), 1) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def fsync_path(path):
    fd = os.open(path, os.O_RDONLY | (os.O_DIRECTORY if path.is_dir() else 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def probe(rw):
    checks = {}
    work = rw / ('.animedb-lab-capability-' + secrets.token_hex(8))
    work.mkdir()
    try:
        a, b, sub = work / 'a.bin', work / 'b.bin', work / 'sub'
        a.write_bytes(b'lab capability source')
        b.write_bytes(b'lab capability collision')
        fsync_path(a); fsync_path(b); fsync_path(work)
        checks['fsync_file_and_directory'] = 'PASS'
        try:
            rename_noreplace(a, b)
            checks['reject_existing_target'] = 'FAIL'
        except OSError as exc:
            checks['reject_existing_target'] = 'PASS' if exc.errno == errno.EEXIST else f'FAIL errno={exc.errno}'
        checks['collision_preserved'] = 'PASS' if b.read_bytes() == b'lab capability collision' else 'FAIL'
        sub.mkdir()
        before = a.stat().st_ino
        rename_noreplace(a, sub / 'c.bin')
        after = (sub / 'c.bin').stat()
        checks['cross_directory_rename_identity'] = 'PASS' if after.st_ino == before and not a.exists() else 'FAIL'
        checks['content_preserved'] = 'PASS' if (sub / 'c.bin').read_bytes() == b'lab capability source' else 'FAIL'
        fsync_path(sub / 'c.bin'); fsync_path(sub); fsync_path(work)
        checks['fsync_after_rename'] = 'PASS'
    finally:
        shutil.rmtree(work)
    checks['artifacts_removed'] = 'PASS' if not work.exists() else 'FAIL'
    return checks


def readonly(ro, share):
    rows = [line.split(' - ') for line in Path('/proc/self/mountinfo').read_text().splitlines()]
    mounted = [(l.split(), r.split()) for l, r in rows if l.split()[4] == str(ro)]
    ok = bool(mounted) and all(r[0] == 'cifs' and r[1] == share and 'ro' in l[5].split(',') for l, r in mounted)
    try:
        (ro / ('.animedb-lab-ro-' + secrets.token_hex(4))).write_bytes(b'x')
        refused = False
    except OSError:
        refused = True
    return {'ro_cifs_view': 'PASS' if ok else 'FAIL', 'ro_write_refused': 'PASS' if refused else 'FAIL'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rw', type=Path, required=True)
    parser.add_argument('--ro', type=Path, required=True)
    parser.add_argument('--share', required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        raise SystemExit('STOP: root required')
    host = coordinator()
    host.require_rw_mount(args.rw, args.share)
    identity = host.host_mount_identity(args.rw, args.share)
    checks = {**probe(args.rw), **readonly(args.ro, args.share)}
    if host.host_mount_identity(args.rw, args.share) != identity:
        raise SystemExit('STOP: RW mount identity changed during the probe')
    report = dict(version=1, lab_only=True, share=args.share, mount_identity=identity, checks=checks,
        note='Lab Samba on the laptop; not evidence for Synology or Nexus.')
    args.report.write_text(json.dumps(report, indent=2) + '\n')
    if any(value != 'PASS' for value in checks.values()):
        print(json.dumps(report, indent=2))
        raise SystemExit('STOP: lab capability probe failed; no runtime evidence written')
    evidence = {key: None for key in host.RUNTIME_KEYS}
    evidence.update(scanner_paused_through_verification=False, inventory_writer_paused_through_verification=False,
        snapshot_capable=False, mount_read_write_checked=True, safe_write_capability_proven=True,
        rename_noreplace_capability_proven=True, mount_identity=identity)
    temporary = args.evidence.with_name('.' + args.evidence.name + '.tmp')
    temporary.write_text(json.dumps(evidence, sort_keys=True, separators=(',', ':')))
    os.chmod(temporary, 0o600)
    os.replace(temporary, args.evidence)
    print(json.dumps(report))


if __name__ == '__main__':
    main()
