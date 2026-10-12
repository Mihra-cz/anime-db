"""Local publication failures must preserve ownership and the primary exception.

Constructor faults run in children so the old leaking implementation cannot
damage pytest or leave descriptors in subsequent tests.
"""
import errno
import gc
import io
import os
from pathlib import Path
import resource
import stat
import subprocess
import sys

import pytest

from app.tools import execution_manifest as cli


KINDS = ('io', 'memory', 'keyboard', 'exit')
CONTENT = '{"title": "Žluťoučký"}'
EXPECTED = (CONTENT + '\n').encode('utf-8')


def _run_in_child(tmp_path, scenario, kind='io', cleanup_kind='io'):
    code = '''
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]).parent))
from test_v6_atomic_local_write import _write_case
_write_case(Path(sys.argv[2]), *sys.argv[3:])
'''
    parent = tmp_path / 'parent-owner'
    parent.write_bytes(b'parent descriptor')
    with parent.open('rb') as stream:
        result = subprocess.run([sys.executable, '-Werror::ResourceWarning', '-c', code,
            str(Path(__file__).resolve()), str(tmp_path), scenario, kind, cleanup_kind],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'DATABASE_URL': 'sqlite://',
                'ANIME_PATH': str(tmp_path / 'library'), 'METADATA_DOWNLOAD_ARTWORK': 'false'},
            capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stderr == ''
        assert result.stdout == f'atomic write verified: {scenario} {kind} {cleanup_kind}\n'
        assert stream.read() == b'parent descriptor'


def test_atomic_local_write_preserves_text_fsync_and_replace_order(tmp_path):
    _run_in_child(tmp_path, 'success')


@pytest.mark.parametrize('scenario', ('early', 'repeated', 'mkstemp', 'write', 'fsync',
                                    'replace', 'replace_after', 'closed_allocation'))
@pytest.mark.parametrize('kind', KINDS)
def test_atomic_local_write_failure_preserves_fd_ownership_and_exception(tmp_path, scenario, kind):
    _run_in_child(tmp_path, scenario, kind)


def test_atomic_local_write_native_text_wrapper_failure_closes_fd(tmp_path):
    _run_in_child(tmp_path, 'late')


@pytest.mark.parametrize('phase', ('write', 'fsync', 'replace'))
@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('cleanup_kind', ('io', 'keyboard', 'exit'))
def test_atomic_local_write_unlink_failure_cannot_mask_primary(tmp_path, phase, kind, cleanup_kind):
    _run_in_child(tmp_path, phase + '_unlink', kind, cleanup_kind)


def test_atomic_local_write_cleanup_failure_without_primary_propagates(tmp_path):
    _run_in_child(tmp_path, 'cleanup_only')


def _fd_inventory():
    result = {}
    for name in os.listdir('/proc/self/fd'):
        try:
            result[int(name)] = os.readlink(f'/proc/self/fd/{name}')
        except FileNotFoundError:
            pass  # os.listdir's own descriptor is already closed.
    return result


def _failure(kind, label, code):
    if kind == 'io':
        return OSError(errno.EIO, label)
    if kind == 'memory':
        return MemoryError(label)
    if kind == 'keyboard':
        return KeyboardInterrupt(label)
    if kind == 'exit':
        return SystemExit(code)
    raise AssertionError(kind)


def _write_case(tmp_path, scenario, kind, cleanup_kind):
    target = tmp_path / 'output.json'
    target.write_bytes(b'OLD target')
    foreign_path = tmp_path / 'foreign-owner'
    foreign_path.write_bytes(b'foreign descriptor')
    primary = _failure(kind, 'primary operation fault', 113)
    cleanup = _failure(cleanup_kind, 'unlink cleanup fault', 117)
    cleanup.add_note('secondary cleanup evidence')
    native_mkstemp, native_open = cli.tempfile.mkstemp, io.open
    native_fsync, native_replace, native_unlink = os.fsync, os.replace, os.unlink
    native_exists = os.path.exists
    created, streams, recycled, events, outcomes = [], [], [], [], []
    armed = False
    constructor_faults = 0
    phase = scenario.removesuffix('_unlink')

    def reject_fileio(event, args):
        nonlocal constructor_faults
        # The native FileIO audit event precedes adoption/opener invocation.
        # mkstemp's os.open event has mode=None and is allowed to allocate.
        if (armed and scenario in {'early', 'repeated'} and event == 'open'
                and args[1] == 'w'
                and (isinstance(args[0], int) or args[0] == str(target))):
            constructor_faults += 1
            raise primary
    sys.addaudithook(reject_fileio)

    def allocate(*args, **kwargs):
        assert not args
        assert kwargs == {'prefix': '.v6-manifest-', 'suffix': '.tmp', 'dir': target.parent}
        events.append('mkstemp')
        if scenario == 'mkstemp':
            raise primary
        fd, name = native_mkstemp(**kwargs)
        temporary = Path(name)
        created.append((fd, temporary))
        assert temporary.parent == target.parent and temporary != target
        assert stat.S_IMODE(os.fstat(fd).st_mode) == 0o600
        assert not os.get_inheritable(fd)
        return fd, name

    class WriteFailure:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def write(self, content):
            # Leave a real partial temporary file; the target is still intact.
            self.stream.write(content[:5])
            self.stream.flush()
            events.append('write')
            raise primary

        def __exit__(self, *args):
            events.append('close')
            return self.stream.__exit__(*args)

    def open_stream(file, *args, **kwargs):
        nonlocal primary
        if (not args or args[0] != 'w'
                or (not isinstance(file, int) and os.fspath(file) != str(target))):
            return native_open(file, *args, **kwargs)
        if scenario == 'late':
            # A native TextIOWrapper fails after FileIO and BufferedWriter exist.
            options = dict(kwargs)
            arguments = list(args)
            if len(arguments) >= 3:  # os.fdopen passes encoding positionally.
                arguments[2] = 'missing-v6-test-codec'
            else:
                options['encoding'] = 'missing-v6-test-codec'
            try:
                native_open(file, *arguments, **options)
            except LookupError as exc:
                primary = exc
            else:
                raise AssertionError('native text construction did not fail')
            replacement = io.FileIO(foreign_path, 'rb')
            recycled.append(replacement)
            assert replacement.fileno() == created[-1][0]
            raise primary
        stream = native_open(file, *args, **kwargs)
        streams.append(stream)
        if scenario == 'closed_allocation':
            retired = stream.fileno()
            stream.close()
            replacement = io.FileIO(foreign_path, 'rb')
            recycled.append(replacement)
            assert replacement.fileno() == retired
            raise primary
        return WriteFailure(stream) if phase == 'write' else stream

    def fsync(fd):
        events.append('fsync')
        assert fd == created[-1][0] and fd == streams[-1].fileno()
        assert not streams[-1].closed
        assert created[-1][1].read_bytes() == EXPECTED
        assert target.read_bytes() == b'OLD target'
        if phase == 'fsync':
            raise primary
        return native_fsync(fd)

    def replace(source, destination):
        events.append('replace')
        assert Path(source) == created[-1][1] and destination == target
        assert streams[-1].closed, 'replace preceded stream close'
        assert Path(source).read_bytes() == EXPECTED
        assert target.read_bytes() == b'OLD target'
        if phase == 'replace':
            raise primary
        native_replace(source, destination)
        assert target.read_bytes() == EXPECTED
        if scenario == 'replace_after':
            # Publication happened, but its acknowledgement was interrupted.
            raise primary

    def unlink(path):
        events.append('unlink')
        assert Path(path) == created[-1][1]
        assert target.read_bytes() == b'OLD target'
        if scenario.endswith('_unlink'):
            raise cleanup
        return native_unlink(path)

    def exists(path):
        if scenario == 'cleanup_only' and created and Path(path) == created[-1][1]:
            # A cleanup check can fail even after successful publication.
            raise cleanup
        return native_exists(path)

    def forbid_numeric_close(fd):
        raise AssertionError('atomic writer attempted numeric close/retry')

    limits = resource.getrlimit(resource.RLIMIT_NOFILE)
    with io.FileIO(foreign_path, 'rb') as foreign:
        before = _fd_inventory()
        try:
            if scenario == 'repeated':
                resource.setrlimit(resource.RLIMIT_NOFILE, (max(before) + 12, limits[1]))
            with pytest.MonkeyPatch.context() as injected:
                injected.setattr(cli.tempfile, 'mkstemp', allocate)
                injected.setattr(io, 'open', open_stream)
                injected.setattr(os, 'fsync', fsync)
                injected.setattr(os, 'replace', replace)
                injected.setattr(os, 'unlink', unlink)
                injected.setattr(os.path, 'exists', exists)
                injected.setattr(os, 'close', forbid_numeric_close)
                armed = True
                for _ in range(32 if scenario == 'repeated' else 1):
                    error = None
                    try:
                        cli._atomic_local_write(target, CONTENT)
                    except BaseException as exc:
                        error = exc
                    outcomes.append(error)
        finally:
            armed = False
            resource.setrlimit(resource.RLIMIT_NOFILE, limits)
        gc.collect()
        after = _fd_inventory()
        expected_fds = {**before, **{s.fileno(): str(foreign_path) for s in recycled}}
        assert after == expected_fds, ('atomic write fd leak', len(after) - len(expected_fds),
            [str(exc) for exc in outcomes if isinstance(exc, OSError) and exc.errno == errno.EMFILE])
        for stream in recycled:
            assert stream.read() == b'foreign descriptor'
            stream.close()  # Only this stream object owns the reused fd.
        assert _fd_inventory() == before
        assert foreign.read() == b'foreign descriptor'
        assert all(stream.closed for stream in streams)

    expected_error = None if scenario == 'success' else cleanup if scenario == 'cleanup_only' else primary
    assert all(error is expected_error for error in outcomes), (scenario, outcomes, expected_error)
    if expected_error is not None:
        assert expected_error.__traceback__ is not None
        if isinstance(expected_error, SystemExit):
            assert expected_error.code == (117 if scenario == 'cleanup_only' else 113)
    if scenario.endswith('_unlink'):
        notes = getattr(primary, '__notes__', ())
        assert any(type(cleanup).__name__ in note and str(cleanup) in note
                   and str(created[-1][1]) in note for note in notes)
        assert 'secondary cleanup evidence' in notes
        assert events.count('unlink') == 1, 'uncertain unlink must not be retried'
        assert created[-1][1].read_bytes() == (CONTENT[:5].encode('utf-8') if phase == 'write' else EXPECTED)
    else:
        assert not list(tmp_path.glob('.v6-manifest-*.tmp'))
    published = scenario in {'success', 'replace_after', 'cleanup_only'}
    assert target.read_bytes() == (EXPECTED if published else b'OLD target')
    assert events.count('replace') == (1 if published or phase == 'replace' else 0)
    if scenario in {'early', 'repeated'}:
        assert constructor_faults == len(outcomes)
        assert not created, 'mkstemp must run inside native FileIO ownership'
    if scenario == 'success':
        assert events == ['mkstemp', 'fsync', 'replace']
    if scenario == 'mkstemp':
        assert events == ['mkstemp'] and not created
    print(f'atomic write verified: {scenario} {kind} {cleanup_kind}')
