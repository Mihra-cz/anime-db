"""Opt-in LOCAL Docker/Chromium test; never selects a remote Docker context.

ANIMEDB_TEST_SERVICE_EDGE=1 pytest tests/test_reorganization_service_edge.py
Requires the already downloaded pinned nginx image, Node ws and Chromium.
"""
import json
from dataclasses import asdict
import hashlib
import http.client
import os
from pathlib import Path
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid

import pytest
from test_reorganization_approval_web import web, case, production

pytestmark = pytest.mark.skipif(os.getenv('ANIMEDB_TEST_SERVICE_EDGE') != '1', reason='explicit local Docker/browser opt-in')
ASSETS = Path(__file__).resolve().parents[1] / 'deploy/nexus/service-edge'
IMAGE = 'nginx:stable-alpine@sha256:583b7f2fce1cdb8da5ad5c39ebd3c33b98b87d493190e548bdee1fa8c619c311'


def docker(*args):
    command = ['docker', '--context', 'default', *args]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f'{args}: {result.stdout}{result.stderr}'
    return result.stdout.strip()


def http_request(url, *, data=None, origin=None):
    request = urllib.request.Request(url, data=data, headers={'Origin': origin} if origin else {})
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as response:
        return response.code, response.read().decode()


@pytest.mark.parametrize('scenario', ['status', 'large', 'application-504', 'post-outage', 'approval'])
def test_same_url_service_fallback_and_truthful_browser_states(tmp_path, request, monkeypatch, scenario):
    assert docker('context', 'inspect', 'default', '--format', '{{.Endpoints.docker.Host}}') == 'unix:///var/run/docker.sock'
    suffix = uuid.uuid4().hex[:12]
    network, backend, edge = (f'anime-service-{suffix}-{name}' for name in ('net', 'app', 'edge'))
    public = tmp_path / 'public'; public.mkdir(mode=0o755)
    assets = tmp_path / 'assets'; assets.mkdir(mode=0o755)
    backend_config = tmp_path / 'backend.conf'
    backend_config.write_text('''pid /tmp/nginx.pid; events {} http { default_type text/html;
      log_format requests '$request_method $uri $status'; access_log /dev/stdout requests;
      client_body_temp_path /tmp/client; proxy_temp_path /tmp/proxy;
      fastcgi_temp_path /tmp/fastcgi; uwsgi_temp_path /tmp/uwsgi; scgi_temp_path /tmp/scgi;
      server { listen 8000;
      location = /health { return 200 'ok'; }
      location = /stop { return 503 'STOP from application'; }
      location = /timeout { add_header Cache-Control 'no-store' always; return 504 '<html><body>AnimeDB preflight timeout</body></html>'; }
      location = /large { alias /assets/large.html; }
      location = /static/check.css { add_header Cache-Control 'public, max-age=3600'; return 200 'body{}'; }
      location = /artwork/check.jpg { add_header Cache-Control 'public, max-age=86400'; return 200 'synthetic-artwork'; }
      location = /echo { if ($http_origin != "http://$http_host") { return 403; } return 200 "$http_host|$http_origin"; }
      location / { return 200 '<html><title>AnimeDB synthetic</title><body>AnimeDB synthetic</body></html>'; }
    }}''')
    backend_config.chmod(0o644)
    # Docker bind traversal uses the final directory itself, not pytest's private parent.
    try:
        # Dedicated bridge with a loopback-only published port, matching Compose.
        docker('network', 'create', network)
        docker('run', '-d', '--pull=never', '--name', backend, '--network', network, '--network-alias', 'app',
            '--read-only', '--tmpfs', '/tmp', '--mount', f'type=bind,src={backend_config},dst=/etc/nginx/nginx.conf,readonly',
            '--mount', f'type=bind,src={assets},dst=/assets,readonly',
            '--entrypoint', 'nginx', IMAGE, '-g', 'daemon off;')
        docker('run', '-d', '--pull=never', '--name', edge, '--network', network, '-p', '127.0.0.1::8080',
            '--user', '101:101', '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
            '--tmpfs', '/tmp:rw,noexec,nosuid,size=8m,mode=1777',
            '--mount', f'type=bind,src={ASSETS},dst=/service,readonly',
            '--mount', f'type=bind,src={public},dst=/public-status,readonly',
            '--entrypoint', 'nginx', IMAGE, '-c', '/service/nginx.conf', '-g', 'daemon off;')
        address = docker('port', edge, '8080/tcp')
        url = 'http://' + address
        for _ in range(40):
            try:
                if http_request(url)[0] == 200: break
            except OSError: pass
            time.sleep(0.25)
        assert http_request(url)[0] == 200
        assert http_request(url + '/echo', data=b'x', origin=url) == (200, address + '|' + url)
        assert http_request(url + '/echo', data=b'x', origin='null')[0] == 403
        assert http_request(url + '/stop') == (503, 'STOP from application')
        if scenario == 'large':
            payload = b'<html><body>' + b'x' * (12_500_000 - 26) + b'</body></html>'
            large = assets / 'large.html'; large.write_bytes(payload); large.chmod(0o644)
            connection = http.client.HTTPConnection('127.0.0.1', int(address.rsplit(':', 1)[1]), timeout=30)
            connection.sock = socket.socket()
            connection.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)
            connection.sock.settimeout(30)
            connection.sock.connect(('127.0.0.1', connection.port))
            try:
                connection.request('GET', '/large')
                response = connection.getresponse()
                assert response.status == 200 and int(response.getheader('Content-Length')) == 12_500_000
                # Stall consumption so a fast upstream would fill the 8 MiB tmpfs.
                time.sleep(1)
                chunks = []
                while chunk := response.read(65536):
                    chunks.append(chunk)
                    time.sleep(0.03)
                received = b''.join(chunks)
                assert len(received) == len(payload)
                assert hashlib.sha256(received).digest() == hashlib.sha256(payload).digest()
                assert 'No space left' not in docker('logs', edge)
                print('large response: 12,500,000 bytes, slow client, exact SHA-256')
            finally:
                connection.close()
            return
        if scenario in ('application-504', 'post-outage'):
            run_browser(tmp_path, url, public, backend, mode=scenario)
            requests = docker('logs', backend).splitlines()
            if scenario == 'application-504':
                assert requests.count('POST /timeout 504') == 1
            else:
                assert not any(line.startswith('POST /submit ') for line in requests)
                assert requests.count('GET /submit 200') == 1
            return
        if scenario == 'approval':
            # Create the existing synthetic fixture after Docker mounts are stable.
            from test_reorganization_approval import snapshot
            from app import target_execution_mounts as mounts, target_execution_filesystem as fs
            from app import maintenance
            from app import reorganization_approval as approval, reorganization_approval_web as approval_web
            import uvicorn
            # Host Docker netns handles are not library mounts. The existing
            # synthetic CIFS fixture runs in this host process, not a worker's
            # container mount namespace. Keep this adaptation test-local.
            def local_records():
                return mounts._parse_mounts('\n'.join(line for line in Path('/proc/self/mountinfo').read_text().splitlines()
                    if ' - nsfs ' not in line))
            monkeypatch.setattr(mounts, '_read_mounts', local_records)
            monkeypatch.setattr(fs, '_read_mounts', local_records)
            monkeypatch.setattr(maintenance, '_read_mounts', local_records)
            def inline_check(database, library_root, inbox, source, final, *, timeout):
                return asdict(approval.check_proposal(approval.read_proposal(inbox, source), final,
                    database=database, library_root=library_root))
            # Run the original RO preflight with the same synthetic mount fixture;
            # its subprocess transport was reviewed separately.
            monkeypatch.setattr(approval_web, 'run_check', inline_check)
            case_data, client, source = request.getfixturevalue('web')
            gateway = json.loads(docker('network', 'inspect', network))[0]['IPAM']['Config'][0]['Gateway']
            sock = socket.socket(); sock.bind((gateway, 0)); sock.listen(128)
            port = sock.getsockname()[1]
            server = uvicorn.Server(uvicorn.Config(client.app, lifespan='off', log_level='error'))
            thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
            thread.start()
            try:
                for _ in range(100):
                    if server.started: break
                    time.sleep(0.05)
                assert server.started
                original = backend_config.read_text()
                route = f'''location /reorganization/approval/ {{ proxy_pass http://{gateway}:{port}; proxy_set_header Host $http_host; }}
      location /static/ {{ proxy_pass http://{gateway}:{port}; proxy_set_header Host $http_host; }}
      '''
                backend_config.write_text(original.replace('location / {', route + 'location / {'))
                previous_workers = set(docker('exec', backend, 'cat', '/proc/1/task/1/children').split())
                docker('exec', backend, 'nginx', '-s', 'reload')
                # Reload is asynchronous; wait until old workers cannot serve
                # the synthetic catch-all instead of the actual approval route.
                for _ in range(100):
                    workers = set(docker('exec', backend, 'cat', '/proc/1/task/1/children').split())
                    if workers and workers.isdisjoint(previous_workers):
                        break
                    time.sleep(0.025)
                else:
                    pytest.fail('old synthetic backend workers did not exit')
                for _ in range(100):
                    if 'manifest-prepare-submit' in http_request(url + f'/reorganization/approval/{source}')[1]:
                        break
                    time.sleep(0.025)
                else:
                    pytest.fail('synthetic approval backend did not become ready')
                before = snapshot(case_data[2], case_data[1].root)
                rejected, body = http_request(url + f'/reorganization/approval/{source}/prepare', data=b'actor=x', origin='null')
                assert rejected == 403 and 'Origin' in body, (rejected, body[:300])
                run_browser(tmp_path, url, public, backend, mode='approval', path=f'/reorganization/approval/{source}')
                assert snapshot(case_data[2], case_data[1].root) == before
                assert len(list(case_data[6].glob('*.approval.json'))) == 1
                assert case_data[0].read_approval(case_data[6], source)['actor'] == 'Edge operator'
            finally:
                server.should_exit = True; thread.join(10); sock.close(); case_data[1].close()
            return
        docker('stop', '-t', '1', backend)
        assert http_request(url)[0] == 503
        assert 'Servisní přehled' in http_request(url)[1]
        assert http_request(url + '/_service/progress.json')[0] == 404
        assert http_request(url + '/_service/nginx.conf')[0] == 404
        config = tmp_path / 'browser.json'
        config.write_text(json.dumps(dict(url=url, public=str(public), directory=str(tmp_path), backend=backend)))
        browser = subprocess.run(['node', str(Path(__file__).with_name('v6_service_edge_browser.cjs')), str(config)],
            capture_output=True, text=True, timeout=110)
        assert browser.returncode == 0, browser.stdout + browser.stderr
        print(browser.stdout)
        print('local edge sample:', docker('stats', '--no-stream', '--format', '{{.CPUPerc}} {{.MemUsage}}', edge))
        assert http_request(url)[0] == 200 and 'AnimeDB synthetic' in http_request(url)[1]
        for path, expected in (('/static/check.css', 'public, max-age=3600'), ('/artwork/check.jpg', 'public, max-age=86400')):
            with urllib.request.urlopen(url + path) as response:
                assert response.headers.get_all('Cache-Control') == [expected]
    finally:
        for name in (edge, backend):
            log = subprocess.run(['docker', '--context', 'default', 'logs', '--tail', '8', name], capture_output=True, text=True, timeout=30)
            print(name, log.stdout, log.stderr)
            subprocess.run(['docker', '--context', 'default', 'rm', '-f', name], capture_output=True, timeout=30)
        subprocess.run(['docker', '--context', 'default', 'network', 'rm', network], capture_output=True, timeout=30)


def run_browser(directory, url, public, backend, **extra):
    config = directory / 'browser.json'
    config.write_text(json.dumps(dict(url=url, public=str(public), directory=str(directory), backend=backend, **extra)))
    browser = subprocess.run(['node', str(Path(__file__).with_name('v6_service_edge_browser.cjs')), str(config)],
        capture_output=True, text=True, timeout=70)
    assert browser.returncode == 0, browser.stdout + browser.stderr
    print(browser.stdout)
