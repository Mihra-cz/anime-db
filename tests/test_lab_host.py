"""LAB wrapper keeps shared release checks for start, trusted selection for down."""
import importlib
from pathlib import Path
import sys

import pytest

from test_nexus_autostart import boot


@pytest.mark.parametrize('action', ['start', 'compose', 'down'])
def test_lab_live_release_drift_blocks_start_but_allows_trusted_down(boot, monkeypatch, action):
    a, config, files, env, src, deployment = boot
    deployment.project_name = 'animedb-v6-lab'
    monkeypatch.setitem(sys.modules, 'reorganization_autostart', a)
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'deploy/nexus'))
    h = importlib.import_module('deploy.lab.lab_host')
    monkeypatch.setattr(h, 'CONFIG', config)
    monkeypatch.setattr(h, 'load_boot', a.load_boot)
    calls = []
    monkeypatch.setattr(h, 'run_command', lambda argv: calls.append(argv) or '')
    monkeypatch.setattr(h, 'ensure_bindings', lambda rows: pytest.fail('no bind before release check or during down'))
    monkeypatch.setattr(sys, 'argv', ['lab_host.py', action] + (['config'] if action == 'compose' else []))
    (src / '.git/HEAD').write_text('f' * 40)
    if action == 'down':
        assert h.main() == 0
        assert calls == [a.compose(deployment) + ['down']]
    else:
        assert h.main() == 2
        assert calls == []
