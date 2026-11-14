"""Host path boundary: real descriptor IO, no production data or Docker."""
import os
from pathlib import Path
import pytest
import sys
sys.path.insert(0, str(Path(__file__).parents[1]))
from deploy.nexus import reorganization_coordinator as c
from deploy.nexus import reorganization_deployment as d


def test_runtime_publication_uses_pinned_directory_after_parent_swap(tmp_path):
    runtime = tmp_path/'runtime'; runtime.mkdir(mode=0o700)
    outside = tmp_path/'outside'; outside.mkdir()
    pin = c.RuntimeDirectory(runtime, os.getuid(), os.getgid())
    runtime.rename(tmp_path/'original')
    runtime.symlink_to(outside, target_is_directory=True)
    pin.write('status.json', {'ok': True}, mode=0o644)
    assert not (outside/'status.json').exists()
    assert (tmp_path/'original/status.json').read_text() == '{"ok":true}'
    with pytest.raises((ValueError,OSError)):
        pin.check()
    pin.close()


def test_runtime_directory_rejects_symlink_ancestors_and_writable_leaf(tmp_path):
    actual = tmp_path/'actual'; actual.mkdir()
    (actual/'leaf').mkdir(mode=0o700)
    (tmp_path/'alias').symlink_to(actual, target_is_directory=True)
    with pytest.raises((ValueError,OSError)):
        c.RuntimeDirectory(tmp_path/'alias/leaf', os.getuid(), os.getgid())
    (actual/'leaf').chmod(0o777)
    with pytest.raises(ValueError):
        c.RuntimeDirectory(actual/'leaf', os.getuid(), os.getgid())


def test_root_input_read_rejects_symlink_in_middle(tmp_path):
    real = tmp_path/'real'; real.mkdir()
    (real/'config').write_text('{}')
    (tmp_path/'alias').symlink_to(real, target_is_directory=True)
    with pytest.raises((ValueError,OSError)):
        c.read_json(tmp_path/'alias/config')


def test_deployment_read_does_not_follow_parent_swapped_during_open(tmp_path, monkeypatch):
    directory = tmp_path/'input'; directory.mkdir()
    (directory/'compose').write_text('approved')
    outside = tmp_path/'outside'; outside.mkdir()
    (outside/'compose').write_text('attacker')
    original = os.open
    def swap(path, flags, *args, **kwargs):
        if path == 'compose' and 'dir_fd' in kwargs:
            directory.rename(tmp_path/'old')
            directory.symlink_to(outside, target_is_directory=True)
        return original(path, flags, *args, **kwargs)
    monkeypatch.setattr(d.os, 'open', swap)
    assert d._read(directory/'compose') == b'approved'


def test_socket_group_is_independent_of_nginx_primary_group():
    root = Path(__file__).parents[1]/'deploy/nexus'
    edge = (root/'service-edge/compose.edge.yaml').read_text()
    unit = (root/'systemd/animedb-reorganization.service').read_text()
    tmpfiles = (root/'tmpfiles.d/animedb-reorganization.conf').read_text()
    assert 'user: "101:101"' in edge
    assert 'group_add:' in edge and 'ANIMEDB_CONTROL_GID' in edge
    assert 'Group=animedb-control' in unit
    assert '0750 root animedb-control' in tmpfiles
    assert 'CAP_SETUID CAP_SETGID' in unit
    assert 'AmbientCapabilities=CAP_SETUID CAP_SETGID' in unit
    assert 'AmbientCapabilities=CAP_SETUID CAP_SETGID' in (root.parent/'lab/lab.sh').read_text()
    assert 'WantedBy=' not in unit


def test_runtime_target_symlink_is_replaced_without_following_it(tmp_path):
    runtime=tmp_path/'runtime';runtime.mkdir(mode=0o700)
    outside=tmp_path/'protected';outside.write_text('unchanged')
    (runtime/'record.json').symlink_to(outside)
    pin=c.RuntimeDirectory(runtime,os.getuid(),os.getgid())
    pin.write('record.json',{'ok':True},exclusive=False)
    assert outside.read_text()=='unchanged'
    assert not (runtime/'record.json').is_symlink()
    assert (runtime/'record.json').stat().st_uid==os.getuid()
    pin.close()


def test_runtime_publication_timeout_is_an_io_error(tmp_path,monkeypatch):
    from deploy.nexus import host_io
    runtime=tmp_path/'runtime';runtime.mkdir(mode=0o700)
    pin=c.RuntimeDirectory(runtime,os.getuid(),os.getgid())
    def timeout(argv,**kwargs):raise host_io.subprocess.TimeoutExpired(argv,kwargs['timeout'])
    monkeypatch.setattr(host_io.subprocess,'run',timeout)
    try:
        with pytest.raises(OSError,match='timed out'):
            pin.write('status.json',{'ok':True})
        assert not (runtime/'status.json').exists()
    finally:pin.close()


@pytest.mark.parametrize('value',['/data#other','"/data#other"',"'/data#other'"])
@pytest.mark.parametrize('key',['source','target'])
def test_ambiguous_hash_in_bind_source_or_target_is_rejected(key,value):
    text='services:\n  app:\n    volumes:\n      - type: bind\n        source: /data\n        target: /data\n        bind:\n          create_host_path: false\n'
    text=text.replace(f'{key}: /data',f'{key}: {value}')
    with pytest.raises(ValueError,match='#'):
        d._bind_sources(text)
