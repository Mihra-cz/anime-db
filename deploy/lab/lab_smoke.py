"""LAB ONLY non-destructive smoke test of the running laptop lab (regular user).

Never creates requests, approvals or decisions and never writes to the library.
Includes one short controlled outage of the lab app container only.
"""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request

LAB = Path('/srv/animedb-v6-lab')
ORIGIN = 'http://127.0.0.1:18080'
REPO = Path(__file__).resolve().parents[2]
APP, EDGE, SMB = 'animedb-v6-lab-app-1', 'animedb-v6-lab-edge-1', 'animedb-v6-lab-smb-smb-1'
results = []


def check(name, condition, detail=''):
    results.append((name, bool(condition), detail))
    print(('PASS ' if condition else 'FAIL ') + name + (f' — {detail}' if detail else ''))


def get(path, timeout=5):
    try:
        with urllib.request.urlopen(ORIGIN + path, timeout=timeout) as response:
            return response.status, response.read().decode('utf-8', 'replace'), dict(response.headers)
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode('utf-8', 'replace'), dict(error.headers)
    except OSError as error:
        return None, str(error), {}


def run(*argv):
    return subprocess.run(argv, capture_output=True, text=True)


def mount(view):
    out = run('findmnt', '-rn', '-M', str(LAB / 'mnt' / view), '-o', 'SOURCE,FSTYPE,VFS-OPTIONS').stdout.split()
    return out


def healthy(deadline):
    while time.monotonic() < deadline:
        status, body, _ = get('/_service/app-health', 3)
        if status == 200 and json.loads(body).get('status') == 'ok':
            return True
        time.sleep(1)
    return False


stack = LAB / 'stack'
config = run('docker', 'compose', '--project-directory', str(stack), '--env-file', str(stack / '.env'),
    '-p', 'animedb-v6-lab', '-f', str(stack / 'compose.yaml'), '-f', str(REPO / 'deploy/lab/compose.edge.yaml'),
    '-f', str(stack / 'compose.v6.yaml'), 'config', '-q')
check('compose configuration valid (three lab sources, project animedb-v6-lab)', config.returncode == 0, config.stderr.strip())
names = set(run('docker', 'ps', '--format', '{{.Names}}').stdout.split())
check('containers running: app, edge, smb', {APP, EDGE, SMB} <= names, ' '.join(sorted(names)))
ports = run('docker', 'port', EDGE).stdout.strip()
check('edge publishes only 127.0.0.1:18080', ports == '8080/tcp -> 127.0.0.1:18080', ports)
check('app publishes no port', run('docker', 'port', APP).stdout.strip() == '')
status, body, _ = get('/health')
check('AnimeDB /health through Nginx', status == 200 and '"ok"' in body, body[:80])
status, body, _ = get('/reorganization')
check('AnimeDB /reorganization page', status == 200 and 'reorganizace' in body.lower(), f'HTTP {status}')
status, body, _ = get('/_service/workflow?source=' + '0' * 64)
check('Nginx static workflow page', status == 200 and 'Servisní přehled' in body, f'HTTP {status}')
check('Nginx hides internal files', get('/_service/nginx.conf')[0] == 404)
status, body, headers = get('/_service/control/cleanup')
check('coordinator reachable via Nginx → Unix socket', status == 200 and json.loads(body) == {'waiting': False}, body[:80])
check('coordinator unit active', run('systemctl', 'is-active', 'animedb-v6-lab-coordinator').stdout.strip() == 'active')
control = Path('/run/animedb-v6-lab')
directory = control.stat()
check('socket directory root:101 0750', (directory.st_uid, directory.st_gid, directory.st_mode & 0o777) == (0, 101, 0o750))
try:
    (control / 'coordinator.sock').stat()
    denied = False
except PermissionError:
    denied = True
check('regular user cannot reach the control socket', denied)
seen = run('docker', 'exec', EDGE, 'stat', '-c', '%u %g %a %F', '/run/animedb-control/coordinator.sock').stdout.strip()
check('edge (UID/GID 101) sees socket root:101 0660', seen == '0 101 660 socket', seen)
with sqlite3.connect(f'file:{LAB}/appdata/db/anime.db?mode=ro', uri=True) as db:
    version = db.execute('PRAGMA user_version').fetchone()[0]
    videos = db.execute('SELECT count(*) FROM videos').fetchone()[0]
check('lab SQLite v9 readable', version == 9 and videos > 0, f'user_version={version} videos={videos}')
ro, rw = mount('ro'), mount('rw')
check('RO view: CIFS //127.0.0.1/animelab, ro', ro[:2] == ['//127.0.0.1/animelab', 'cifs'] and 'ro' in ro[2].split(','), ' '.join(ro))
check('RW view: CIFS //127.0.0.1/animelab, rw', rw[:2] == ['//127.0.0.1/animelab', 'cifs'] and 'rw' in rw[2].split(','), ' '.join(rw))
check('RO and RW are separate CIFS superblocks', (LAB / 'mnt/ro').stat().st_dev != (LAB / 'mnt/rw').stat().st_dev)
check('RO view refuses writes (statvfs)', os.statvfs(LAB / 'mnt/ro').f_flag & os.ST_RDONLY)
check('RO and RW show the same library', sorted(p.name for p in (LAB / 'mnt/ro').iterdir()) == sorted(p.name for p in (LAB / 'mnt/rw').iterdir()))
inspect = json.loads(run('docker', 'inspect', APP).stdout)[0]
media = next(m for m in inspect['Mounts'] if m['Destination'] == '/media/anime')
check('app binds only the RO view at /media/anime', media['Source'] == str(LAB / 'mnt/ro') and media['RW'] is False)
check('app has no RW library, socket or ledger bind', not any(m['Source'].startswith((str(LAB / 'mnt/rw'), '/run/animedb', str(LAB / 'appdata/approval/ledger'))) or 'docker.sock' in m['Source'] for m in inspect['Mounts']))
edge_mounts = {m['Destination']: (m['Source'], m['RW']) for m in json.loads(run('docker', 'inspect', EDGE).stdout)[0]['Mounts']}
check('edge has only three RO binds (static, public status, socket dir)', set(edge_mounts) == {'/service', '/public-status', '/run/animedb-control'}
    and not any(rw for _, rw in edge_mounts.values()), json.dumps(edge_mounts))

# Short controlled outage of the lab app only; Nginx must keep serving.
run('docker', 'stop', '-t', '10', APP)
status, body, _ = get('/')
check('outage: Nginx answers 503 with the service page', status == 503 and 'Servisní přehled' in body, f'HTTP {status}')
check('outage: workflow page and control endpoint stay available', get('/_service/workflow')[0] == 200 and get('/_service/control/cleanup')[0] == 200)
check('outage: app-health reports unavailable', get('/_service/app-health')[0] in (502, 503, 504))
run('docker', 'start', APP)
check('app returns after the outage', healthy(time.monotonic() + 60))
check('coordinator stayed idle (no requests, claims or status)', not any((LAB / 'appdata/approval/outbox').iterdir()))

failed = [name for name, ok, _ in results if not ok]
print(f'\nSMOKE: {len(results) - len(failed)}/{len(results)} PASS' + (f'; FAIL: {failed}' if failed else ''))
raise SystemExit(1 if failed else 0)
