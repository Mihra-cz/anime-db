"""Regression checks for LAB shell behavior, without Docker/systemd changes."""
import grp
import os
from pathlib import Path
import subprocess
import socket

import pytest

ROOT = Path(__file__).resolve().parents[1]


def functions(tmp_path):
    raw = (ROOT / 'deploy/lab/lab.sh').read_text()
    path = tmp_path / 'functions.sh'
    path.write_text(raw[:raw.rindex('\ncase "${1:-}" in')])
    return path


def test_image_tag_is_identical_in_c_and_czech_locale(tmp_path):
    locales = subprocess.check_output(['locale', '-a'], text=True).lower()
    if 'cs_cz.utf8' not in locales:
        pytest.skip('Czech locale required for the reported regression')
    script = functions(tmp_path)
    values = []
    for locale in ('C', 'cs_CZ.UTF-8'):
        result = subprocess.run(['bash', '-c', 'source "$1"; REPO="$2"; image_tag',
            'lab-test', str(script), str(ROOT)], env=dict(os.environ, LC_ALL=locale),
            capture_output=True, text=True, check=True)
        values.append(result.stdout.strip())
    assert values[0] == values[1]


def test_missing_control_group_stops_before_any_systemd_dispatch(tmp_path):
    gid = 2147483646
    try:
        grp.getgrgid(gid)
    except KeyError:
        pass
    else:
        pytest.skip('test GID is allocated on this host')
    script = functions(tmp_path)
    marker = tmp_path / 'systemd-called'
    result = subprocess.run(['bash', '-c',
        'source "$1"; CONTROL_GID=2147483646; '
        'systemctl() { printf called > "$LAB_TEST_MARKER"; return 0; }; '
        'systemd-run() { printf called > "$LAB_TEST_MARKER"; }; start_coordinator',
        'lab-test', str(script)], env=dict(os.environ, LAB_TEST_MARKER=str(marker)),
        capture_output=True, text=True)
    assert result.returncode != 0
    assert 'control group' in result.stderr.lower()
    assert not marker.exists()


@pytest.mark.parametrize('active', [True, False])
def test_coordinator_ready_requires_active_unit_even_with_stale_socket(tmp_path, active):
    script = functions(tmp_path)
    marker = tmp_path / 'http-called'
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(tmp_path / 'coordinator.sock'))
        result = subprocess.run(['bash', '-c',
            'source "$1"; RUN="$2"; systemctl() { return "$LAB_TEST_UNIT_STATUS"; }; '
            'curl() { printf called > "$LAB_TEST_MARKER"; }; coordinator_ready',
            'lab-test', str(script), str(tmp_path)],
            env=dict(os.environ, LAB_TEST_UNIT_STATUS='0' if active else '1', LAB_TEST_MARKER=str(marker)),
            capture_output=True, text=True)
    assert result.returncode == (0 if active else 1)
    assert marker.exists() is active
