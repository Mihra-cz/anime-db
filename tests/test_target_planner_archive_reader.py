"""Archive ownership faults stay in bounded children over pytest scratch files."""
import errno
import gc
import hashlib
import io
import os
from pathlib import Path
import resource
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest

from app import target_planner_filesystem as filesystem
from app.target_planner_types import FilesystemEntry, FilesystemSnapshot


KINDS = ('io', 'memory', 'keyboard', 'exit')
STAMP = 1_700_000_000_123_456_789
ABC_SHA256 = 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad'


def _run_in_child(tmp_path, scenario, kind='io'):
    code = '''
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]).parent))
from test_target_planner_archive_reader import _archive_case
_archive_case(Path(sys.argv[2]), *sys.argv[3:])
'''
    parent = tmp_path / 'parent-owner'
    parent.write_bytes(b'parent descriptor')
    with parent.open('rb') as stream:
        result = subprocess.run([sys.executable, '-Werror::ResourceWarning', '-c', code,
            str(Path(__file__).resolve()), str(tmp_path), scenario, kind],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'DATABASE_URL': 'sqlite://',
                'ANIME_PATH': str(tmp_path / 'library'), 'METADATA_DOWNLOAD_ARTWORK': 'false'},
            capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stderr == ''
        # A silent early return must not pass: the helper reports completion last.
        assert result.stdout == f'archive case verified: {scenario} {kind}\n'
        assert stream.read() == b'parent descriptor'


@pytest.mark.parametrize('extension', ('zip', 'rar', '7z', 'ZIP', 'non_utf8'))
def test_archive_snapshot_keeps_sha256_metadata_and_readonly_open(tmp_path, extension):
    _run_in_child(tmp_path, 'normal', extension)


@pytest.mark.parametrize('kind', KINDS)
def test_archive_early_fileio_failure_cannot_leak_descriptor(tmp_path, kind):
    _run_in_child(tmp_path, 'early', kind)


def test_archive_open_failure_is_inaccessible_without_allocating_fd(tmp_path):
    _run_in_child(tmp_path, 'open_error')


def test_archive_native_buffer_allocation_failure_closes_owned_fd(tmp_path):
    _run_in_child(tmp_path, 'buffer_error', 'memory')


@pytest.mark.parametrize('kind', KINDS)
def test_archive_closed_allocation_preserves_recycled_foreign_owner(tmp_path, kind):
    _run_in_child(tmp_path, 'closed_allocation', kind)


@pytest.mark.parametrize('kind', KINDS)
def test_archive_hash_failure_closes_stream_and_preserves_exception(tmp_path, kind):
    _run_in_child(tmp_path, 'hash_error', kind)


@pytest.mark.parametrize('scenario', ('directory_swap', 'symlink_leaf', 'symlink_swap', 'nonregular_stat'))
def test_archive_rejects_unsafe_leaf_without_leaking_fd(tmp_path, scenario):
    _run_in_child(tmp_path, scenario)


@pytest.mark.parametrize('scenario', ('content_change', 'same_size_change', 'mtime_change', 'pre_open_change'))
def test_archive_changed_during_inventory_retains_inaccessible_diagnostic(tmp_path, scenario):
    _run_in_child(tmp_path, scenario)


@pytest.mark.parametrize('kind', KINDS)
def test_repeated_archive_allocation_failures_cannot_exhaust_fd_limit(tmp_path, kind):
    _run_in_child(tmp_path, 'repeated', kind)


def test_subs_archive_is_preserved_without_opening_or_hashing(tmp_path):
    _run_in_child(tmp_path, 'subs')


def _fd_inventory():
    result = {}
    for name in os.listdir('/proc/self/fd'):
        try:
            result[int(name)] = os.readlink(f'/proc/self/fd/{name}')
        except FileNotFoundError:
            pass  # The descriptor used by os.listdir has already been closed.
    return result


def _failure(kind):
    if kind == 'io':
        return OSError(errno.EIO, 'archive fault')
    if kind == 'memory':
        return MemoryError('archive fault')
    if kind == 'keyboard':
        return KeyboardInterrupt('archive fault')
    if kind == 'exit':
        return SystemExit(113)


def _archive_case(tmp_path, scenario, kind):
    library = tmp_path / 'library'
    library.mkdir(mode=0o700)
    directory = library / 'Subs' if scenario == 'subs' else library
    if directory != library:
        directory.mkdir()
    name = ('name-\udcff.zip' if kind == 'non_utf8' else
            'archive with spaces.' + kind if scenario == 'normal' else 'archive.zip')
    archive = directory / name
    archive.write_bytes(b'abc')
    os.utime(archive, ns=(STAMP, STAMP))
    directory_mtime = directory.stat().st_mtime_ns
    relative = archive.relative_to(library).as_posix()
    foreign_path = tmp_path / 'foreign-owner'
    foreign_path.write_bytes(b'foreign descriptor')
    if scenario == 'symlink_leaf':
        archive.unlink()
        archive.symlink_to(foreign_path)

    primary = _failure(kind)
    native_open, native_io_open = os.open, io.open
    native_digest, native_fstat = hashlib.file_digest, os.fstat
    opens, streams, hashes, recycled = [], [], [], []
    armed = False

    def reject_fileio(event, args):
        # FileIO's audit event precedes adoption/opener invocation. os.open's
        # event has mode=None, so it still allocates in the old leaking reader.
        if (armed and scenario in {'early', 'repeated'} and event == 'open'
                and args[1] is not None
                and (isinstance(args[0], int) or args[0] == str(archive))):
            raise primary
    sys.addaudithook(reject_fileio)

    def opened(path, flags, *args, **kwargs):
        assert path == str(archive), 'reader changed the source path'
        assert flags & os.O_NOFOLLOW
        assert flags & os.O_ACCMODE == os.O_RDONLY
        assert not flags & (os.O_CREAT | os.O_TRUNC | os.O_APPEND)
        opens.append(None)
        if scenario == 'open_error':
            raise primary
        if scenario in {'directory_swap', 'symlink_swap'}:
            archive.rename(tmp_path / 'original-archive')
            if scenario == 'directory_swap':
                archive.mkdir()
            else:
                archive.symlink_to(foreign_path)
        if scenario == 'pre_open_change':
            os.utime(archive, ns=(STAMP, STAMP + 1))
        fd = native_open(path, flags, *args, **kwargs)
        opens[-1] = fd
        return fd

    def open_stream(file, *args, **kwargs):
        nonlocal primary
        if not isinstance(file, int) and os.fspath(file) != str(archive):
            return native_io_open(file, *args, **kwargs)
        if scenario == 'buffer_error':
            # A real BufferedReader allocation fails after FileIO adopted fd.
            # Only this isolated child has its virtual-memory limit changed.
            limits = resource.getrlimit(resource.RLIMIT_AS)
            pages = int(Path('/proc/self/statm').read_text().split()[0])
            cap = pages * os.sysconf('SC_PAGE_SIZE') + 32 * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (cap, limits[1]))
            try:
                native_io_open(file, 'rb', buffering=128 * 1024 * 1024,
                               opener=kwargs.get('opener'))
            except MemoryError as exc:
                primary = exc
            else:
                raise AssertionError('native buffer allocation did not fail')
            finally:
                resource.setrlimit(resource.RLIMIT_AS, limits)
            replacement = io.FileIO(foreign_path, 'rb')
            recycled.append(replacement)
            assert replacement.fileno() == opens[-1]
            raise primary
        stream = native_io_open(file, *args, **kwargs)
        streams.append(stream)
        if scenario == 'closed_allocation':
            retired = stream.fileno()
            stream.close()
            replacement = io.FileIO(foreign_path, 'rb')
            recycled.append(replacement)
            assert replacement.fileno() == retired
            raise primary
        return stream

    def digest(stream, algorithm):
        hashes.append(algorithm)
        assert algorithm == 'sha256'
        if scenario == 'hash_error':
            raise primary
        result = native_digest(stream, algorithm)
        if scenario in {'content_change', 'same_size_change'}:
            with io.FileIO(archive, 'wb') as changed:
                changed.write(b'abcd' if scenario == 'content_change' else b'xyz')
            os.utime(archive, ns=(STAMP, STAMP + 1))
        if scenario == 'mtime_change':
            os.utime(archive, ns=(STAMP, STAMP + 1))
        return result

    def fstat(fd):
        current = native_fstat(fd)
        if scenario == 'nonregular_stat':
            return SimpleNamespace(st_mode=stat.S_IFIFO, st_size=current.st_size,
                                   st_mtime_ns=current.st_mtime_ns)
        return current

    def forbid_numeric_close(fd):
        raise AssertionError('archive reader attempted numeric close/retry')

    nofile = resource.getrlimit(resource.RLIMIT_NOFILE)
    with io.FileIO(foreign_path, 'rb') as foreign:
        before = _fd_inventory()
        outcomes = []
        try:
            if scenario == 'repeated':
                resource.setrlimit(resource.RLIMIT_NOFILE, (max(before) + 12, nofile[1]))
            with pytest.MonkeyPatch.context() as injected:
                injected.setattr(os, 'open', opened)
                injected.setattr(os, 'close', forbid_numeric_close)
                injected.setattr(io, 'open', open_stream)
                injected.setattr(hashlib, 'file_digest', digest)
                injected.setattr(os, 'fstat', fstat)
                armed = True
                for _ in range(64 if scenario == 'repeated' else 1):
                    snapshot = error = None
                    try:
                        snapshot = filesystem.inventory_filesystem(library)
                    except BaseException as exc:
                        error = exc
                    outcomes.append((snapshot, error))
        finally:
            armed = False
            resource.setrlimit(resource.RLIMIT_NOFILE, nofile)
        gc.collect()
        expected_fds = {**before, **{stream.fileno(): str(foreign_path) for stream in recycled}}
        after = _fd_inventory()
        diagnostics = sorted({entry.diagnostic for snapshot, _ in outcomes if snapshot
                              for entry in snapshot.entries if entry.diagnostic})
        assert after == expected_fds, ('archive fd leak', len(after) - len(expected_fds), diagnostics)
        for stream in recycled:
            assert stream.read() == b'foreign descriptor'
            stream.close()  # This object owns the deliberate foreign allocation.
        assert _fd_inventory() == before
        assert foreign.read() == b'foreign descriptor'
        assert all(stream.closed for stream in streams)

        for snapshot, error in outcomes:
            if scenario in {'early', 'repeated', 'hash_error', 'closed_allocation', 'buffer_error'} and kind != 'io':
                assert error is primary and error.__traceback__ is not None
                if kind == 'exit':
                    assert error.code == 113
                continue
            assert error is None
            if scenario == 'normal':
                entries = (FilesystemEntry(relative, 'regular', 3, STAMP, content_sha256=ABC_SHA256),)
            elif scenario == 'subs':
                entries = (FilesystemEntry('Subs', 'directory', mtime_ns=directory_mtime),
                           FilesystemEntry(relative, 'regular', 3, STAMP))
            elif scenario == 'symlink_leaf':
                entries = (FilesystemEntry(relative, 'symlink'),)
            else:
                code = (errno.EISDIR if scenario == 'directory_swap' else
                        errno.ELOOP if scenario == 'symlink_swap' else
                        None if scenario in {'content_change', 'same_size_change', 'mtime_change',
                                             'pre_open_change', 'nonregular_stat'} else errno.EIO)
                entries = (FilesystemEntry(relative, 'inaccessible', diagnostic=f'stat_or_read_error:{code}'),)
            assert snapshot == FilesystemSnapshot(str(library), entries,
                os.pathconf(library, 'PC_NAME_MAX'), os.pathconf(library, 'PC_PATH_MAX'), ())
        if scenario in {'early', 'repeated', 'symlink_leaf', 'subs'}:
            assert opens == []
        else:
            assert len(opens) == 1
        assert hashes == (['sha256'] if scenario in {'normal', 'hash_error', 'content_change',
            'same_size_change', 'mtime_change', 'pre_open_change'} else [])
    # Any leak in a failing baseline is released by process exit, never by
    # guessing whether recorded numeric fds still belong to this test.
    print(f'archive case verified: {scenario} {kind}')
