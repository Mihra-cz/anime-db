"""Post-rename server metadata sync never substitutes a weaker checkpoint."""
import ctypes
import errno
import os

import pytest

from app import target_execution_filesystem as fs


@pytest.mark.parametrize('outcome', ['success', 'unsupported', 'missing_ctime', 'interrupted'])
def test_descriptor_force_sync_contract_has_no_fallback_or_retry(tmp_path, monkeypatch, outcome):
    source = tmp_path / 'new-file'
    source.write_bytes(b'isolated')
    calls = []
    def statx(fd, path, flags, mask, result):
        calls.append(fd)
        assert path == b'' and flags == 0x1000 | 0x2000
        assert mask == 0x3df  # exact checkpoint fields, excluding unused atime/blocks
        assert ctypes.sizeof(result) == 256
        if outcome == 'interrupted':
            raise InterruptedError('stop, do not retry')
        ctypes.cast(result, ctypes.POINTER(ctypes.c_uint32))[0] = (
            mask & ~0x80 if outcome == 'missing_ctime' else mask)
        ctypes.set_errno(errno.EOPNOTSUPP)
        return -1 if outcome == 'unsupported' else 0

    monkeypatch.setattr(fs, '_load_statx', lambda: statx, raising=False)
    with source.open('rb') as opened:
        if outcome == 'success':
            assert isinstance(fs._sync_file_metadata(opened.fileno()), os.stat_result)
        else:
            with pytest.raises(OSError):
                fs._sync_file_metadata(opened.fileno())
    assert len(calls) == 1
    assert source.read_bytes() == b'isolated'


def test_real_force_sync_on_a_new_local_file_preserves_exact_stat(tmp_path):
    source = tmp_path / 'local-file'
    source.write_bytes(b'isolated local statx ABI check')
    with source.open('rb') as opened:
        before = os.fstat(opened.fileno())
        fs._sync_file_metadata(opened.fileno())
        after = os.fstat(opened.fileno())
    fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns',
        'st_mode', 'st_nlink', 'st_uid', 'st_gid')
    assert all(getattr(before, field) == getattr(after, field) for field in fields)
