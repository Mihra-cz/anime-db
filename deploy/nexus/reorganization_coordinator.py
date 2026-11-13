#!/usr/bin/env python3
"""Host-only V6 transport and lifecycle. E7 remains the only execution engine.

Only trusted deployment configuration determines executables, images and mounts.
Requests and cleanup decisions are data. Spent claims are never retried.
"""
from dataclasses import dataclass, fields
from datetime import datetime, timezone
import argparse
import ctypes
import fcntl
import hashlib
import hmac
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import re
import secrets
import socketserver
import stat
import subprocess
import threading
import time
from urllib.parse import urlsplit, parse_qs
from urllib.request import urlopen

try:
    from host_io import RuntimeDirectory, open_file, ensure_bindings
except ImportError:
    from deploy.nexus.host_io import RuntimeDirectory, open_file, ensure_bindings

RUNTIME_DIRECTORIES = ('database_dir','state_dir','inbox','outbox','ledger','backups','service_status')

MAX_REQUEST = 64 * 1024
MAX_PROPOSAL = 64 * 1024 * 1024
MAX_DECISION = 2 * 1024 * 1024
HEARTBEAT_SECONDS = 5
MAX_AGE_SECONDS = 900
MAX_DIAGNOSTIC = 16 * 1024
SHA = re.compile(r'[0-9a-f]{64}\Z')
REQUEST = re.compile(r'([0-9a-f]{64})\.(prepare|start)-request\.json\Z')
CLAIM = re.compile(r'([0-9a-f]{64})\.(prepare|start)-claim\.json\Z')
PHASES = ('manifest', 'folders', 'rename', 'sqlite', 'cleanup', 'verification')
COOKIE = 'animedb_cleanup'
PATH_FIELDS = ('database_dir', 'state_dir', 'media_ro', 'media_rw', 'inbox', 'outbox',
    'ledger', 'backups', 'service_status', 'control_dir', 'private_dir', 'worker_config',
    'snapshot_hook', 'writers_stop_hook', 'writers_start_hook', 'runtime_evidence')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate JSON member')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError('non-finite JSON')))


def read_json(path, limit=MAX_REQUEST, *, fd=None):
    fd = open_file(path) if fd is None else fd
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
            raise ValueError('unsafe or oversized record')
        raw = os.pread(fd, limit + 1, 0)
        after = os.fstat(fd)
        if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) or len(raw) != before.st_size:
            raise ValueError('record changed while reading')
        return strict_json(raw)
    finally:
        os.close(fd)


def write_json(path, value, *, exclusive=False, mode=0o600, owner=None):
    """Publish complete fsynced bytes; create-once records use RENAME_NOREPLACE."""
    temporary = path.with_name('.' + path.name + '.' + secrets.token_hex(12))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode)
    try:
        # systemd uses UMask=0077; the public derived snapshot still needs its
        # explicit 0644 policy so the independent edge UID can read it.
        os.fchmod(fd,mode)
        if owner is not None and os.geteuid() == 0:
            os.fchown(fd, *owner)
        raw = canonical(value)
        with os.fdopen(fd, 'wb', closefd=False) as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(fd)
        if exclusive:
            rename = ctypes.CDLL(None, use_errno=True).renameat2
            rename.argtypes = [ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint]
            rename.restype = ctypes.c_int
            if rename(-100,os.fsencode(temporary),-100,os.fsencode(path),1) != 0:
                code = ctypes.get_errno()
                raise OSError(code,os.strerror(code),str(path))
        else:
            os.replace(temporary, path)
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        os.close(fd)
        if temporary.exists():
            temporary.unlink()


def secure_root_path(path, *, executable=False):
    """Protect every component from replacement by app/edge/users."""
    if not path.is_absolute() or '..' in path.parts or ',' in str(path) or '\n' in str(path):
        raise ValueError('canonical absolute deployment path required')
    for component in reversed((path, *path.parents)):
        info = component.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError('deployment configuration must be root controlled')
    if not path.is_file() or (executable and not os.access(path, os.X_OK)):
        raise ValueError('regular deployment file/executable required')


@dataclass(frozen=True)
class Config:
    version: int
    image: str
    uid: int
    gid: int
    public_origin: str
    compose_files: tuple[Path, Path, Path]
    worker_config: Path
    snapshot_hook: Path
    writers_stop_hook: Path
    writers_start_hook: Path
    socket_gid: int
    nas_source: str
    database_dir: Path
    state_dir: Path
    media_ro: Path
    media_rw: Path
    inbox: Path
    outbox: Path
    ledger: Path
    backups: Path
    service_status: Path
    control_dir: Path
    private_dir: Path
    runtime_evidence: Path | None = None
    app_health_timeout_seconds: int = 900
    approved_root: Path | None = None

    def __post_init__(self):
        if type(self.app_health_timeout_seconds) is not int or not 0 < self.app_health_timeout_seconds <= 3600:
            raise ValueError('app_health_timeout_seconds must be an integer in [1, 3600]')


def load_config(path):
    path = Path(path)
    secure_root_path(path)
    value = read_json(path)
    names = {f.name for f in fields(Config)}
    if (type(value) is not dict or set(value) - names
            or names - {'app_health_timeout_seconds','approved_root'} - set(value)):
        raise ValueError('exact coordinator config fields required')
    if type(value['version']) is not int or value['version'] != 1:
        raise ValueError('config version 1 required')
    if not re.fullmatch(r'[a-zA-Z0-9./:_-]+(?::v6-[0-9a-f]{7,40}|@sha256:[0-9a-f]{64})', value['image']):
        raise ValueError('explicit versioned worker/app image required')
    for name in ('uid', 'gid', 'socket_gid'):
        if type(value[name]) is not int or not 0 < value[name] < 2**31:
            raise ValueError('non-root numeric worker/edge identities required')
    origin = urlsplit(value['public_origin'])
    if (origin.scheme not in ('http', 'https') or not origin.hostname or origin.username or origin.password
            or origin.path or origin.query or origin.fragment or value['public_origin'].endswith('/')):
        raise ValueError('exact public Origin required')
    if type(value['nas_source']) is not str or not re.fullmatch(r'//[^/\s]+/[^/\s]+', value['nas_source']):
        raise ValueError('exact CIFS share required')
    for name in PATH_FIELDS:
        if type(value[name]) is not str:
            raise ValueError('absolute deployment paths required')
        value[name] = Path(value[name])
        current = value[name]
        if not current.is_absolute() or '..' in current.parts or any(c in str(current) for c in (',', '\n', '\r')):
            raise ValueError('canonical Docker bind paths required')
        for part in (current, *current.parents):
            if part.is_symlink():
                raise ValueError('symlink deployment paths rejected')
    if type(value['compose_files']) is not list or len(value['compose_files']) != 3:
        raise ValueError('the three production Compose files in order are required')
    value['compose_files'] = tuple(Path(p) for p in value['compose_files'])
    if value.get('approved_root') is not None:
        value['approved_root'] = Path(value['approved_root'])
    resolve_deployment(value['compose_files'], approved_root=value.get('approved_root'))
    for item in (value['worker_config'], value['runtime_evidence']):
        secure_root_path(item)
    for name in ('snapshot_hook', 'writers_stop_hook', 'writers_start_hook'):
        secure_root_path(value[name], executable=True)
    directories = [value[name] for name in PATH_FIELDS if name not in (
        'worker_config', 'snapshot_hook', 'writers_stop_hook', 'writers_start_hook', 'runtime_evidence')]
    for directory in directories:
        if not directory.is_dir():
            raise ValueError('all deployment directories must already exist')
    if any(a == b or a.is_relative_to(b) or b.is_relative_to(a)
            for i,a in enumerate(directories) for b in directories[i+1:]):
        raise ValueError('separate, disjoint deployment directories required')
    for name in ('private_dir', 'control_dir'):
        info = value[name].stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError('private/control directories must be root controlled')
    if stat.S_IMODE(value['private_dir'].stat().st_mode) != 0o700:
        raise ValueError('host claims require private mode 0700')
    for directory in directories:
        runtime = directory in [value[name] for name in RUNTIME_DIRECTORIES]
        for parent in directory.parents:
            info = parent.lstat()
            allowed = {0, value['uid']} if runtime and value.get('approved_root') is not None else {0}
            if info.st_uid not in allowed or info.st_mode & 0o022:
                raise ValueError('deployment directory ancestors have unsafe ownership or permissions')
    for name in ('database_dir','state_dir','inbox','outbox','ledger','backups','service_status'):
        info = value[name].stat()
        if info.st_uid != value['uid'] or info.st_mode & 0o022:
            raise ValueError('worker evidence directories require runtime ownership without group/other writes')
    if value.get('approved_root') is not None:
        if value['socket_gid'] in (101,value['gid']) or value['control_dir'].stat().st_gid != value['socket_gid']:
            raise ValueError('dedicated control socket GID must match control directory and differ from app/nginx groups')
    config = Config(**value)
    verify_worker_config(config)
    return config


def verify_worker_config(config):
    data = read_json(config.worker_config)
    expected = dict(database='/data/anime.db',library_root='/media/anime',share=config.nas_source,share_root='/',
        inbox='/approval-inbox',outbox='/approval-outbox',ledger='/approval-ledger',
        runtime_lock='/state/runtime.lock',backups='/backups',service='/service-status')
    flags = {'mount_read_write_checked','safe_write_capability_proven','rename_noreplace_capability_proven'}
    if (type(data) != dict or set(data) != set(expected)|flags|{'windows_root'}
            or any(data.get(k) != v for k,v in expected.items())
            or any(type(data[k]) != bool for k in flags)
            or type(data['windows_root']) != str or not data['windows_root'].strip()):
        raise ValueError('worker configuration must match fixed container bindings')


class CommandFailed(RuntimeError):
    """Bounded private subprocess diagnostics; public messages use only codes."""
    def __init__(self, returncode, stdout='', stderr=''):
        super().__init__('fixed integration command failed')
        self.returncode = returncode
        self.stdout = stdout[-MAX_DIAGNOSTIC:]
        self.stderr = stderr[-MAX_DIAGNOSTIC:]


PUBLIC_ERRORS = {
    'RW_MOUNT_UNAVAILABLE':'Samostatný RW mount není dostupný; je nutná kontrola připojení správcem.',
    'CAPABILITIES_UNVERIFIED':'Pro aktuální RW mount chybí platné ověření bezpečného zápisu a přejmenování.',
    'SNAPSHOT_FAILED':'Nepodařilo se připravit platný snapshot vázaný k manifestu.',
    'MANIFEST_EXPIRED':'Platnost manifestu vypršela; připravte nový manifest a nové schválení.',
    'REQUEST_EXPIRED':'Požadavek ve frontě vypršel; vytvořte nový náhled a schválení.',
    'DATABASE_CHANGED':'Databáze se od plánu změnila; připravte nový náhled a manifest.',
    'PLAN_UNRESOLVED':'Plán obsahuje nevyřešené REVIEW/BLOCKED; opravte uložené autority a vytvořte nový náhled.',
    'APPROVAL_INVALID':'Schválení neodpovídá přesnému manifestu nebo již bylo spotřebováno.',
    'EVIDENCE_UNCERTAIN':'Stav provozních dokladů není jistý; nic neopakujte a zachovejte všechny doklady.',
    'WRITERS_NOT_STOPPED':'Zastavení všech zapisovatelů není potvrzené; reorganizace nezačala.',
    'WORKER_FAILED':'Worker skončil chybou; podrobnosti zůstávají v soukromém reportu správce.',
    'APP_RESTART_FAILED':'Restart aplikace selhal; dostupnost ani spuštění zapisovatelů nejsou potvrzené.',
    'APP_RESTART_TIMED_OUT':'Vypršel maximální čas pro dostupnost aplikace; další automatický start se neprovádí.',
}


def diagnostic_code(error, fallback):
    for line in getattr(error, 'stderr', '').splitlines()[::-1]:
        if line.startswith('STOP_JSON:'):
            try:
                record = strict_json(line[len('STOP_JSON:'):])
                code = record.get('error_code')
                if record.get('version') == 1 and code in PUBLIC_ERRORS:
                    return code
            except (ValueError, TypeError, AttributeError):
                pass
    return getattr(error, 'error_code', fallback)


def failure(code):
    error = ValueError(code)
    error.error_code = code
    return error


def run_command(argv, *, input=None, timeout=120):
    try:
        result = subprocess.run(argv, input=input, text=True, capture_output=True, check=False,
            timeout=timeout, env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin', 'HOME':'/root', 'LANG':'C.UTF-8'})
    except subprocess.TimeoutExpired as exc:
        decode = lambda value: value.decode('utf-8', 'replace') if isinstance(value, bytes) else value or ''
        raise CommandFailed(None, decode(exc.stdout), decode(exc.stderr)) from exc
    if result.returncode:
        raise CommandFailed(result.returncode, result.stdout, result.stderr)
    if len(result.stdout) > MAX_REQUEST:
        raise CommandFailed(result.returncode, result.stdout, 'oversized integration response')
    return result.stdout


def resolve_deployment(compose_files, *, approved_root=None):
    # Installed host helper is a trusted sibling; repo tests use its package path.
    try:
        from reorganization_deployment import resolve_deployment as resolve
    except ImportError:
        from deploy.nexus.reorganization_deployment import resolve_deployment as resolve
    return resolve(compose_files, approved_root=approved_root)


def check_health(url):
    try:
        with urlopen(url, timeout=2) as response:
            data = strict_json(response.read(MAX_REQUEST))
            return response.status == 200 and data.get('status') == 'ok'
    except (OSError, ValueError):
        return False


def require_rw_mount(path, nas_source):
    """No fallback to an unmounted local directory or to the scanner's RO view."""
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        before, _, after = line.partition(' - ')
        left, right = before.split(), after.split()
        if len(left) >= 6 and len(right) >= 3 and left[4] == str(path):
            if right[0] == 'cifs' and right[1] == nas_source and 'rw' in left[5].split(',') and 'rw' in right[2].split(','):
                return
    raise ValueError('isolated RW CIFS mount is not confirmed')



RUNTIME_KEYS = {'scanner_paused_through_verification','inventory_writer_paused_through_verification',
    'snapshot_id','snapshot_at','snapshot_manifest_id','snapshot_capable','db_backup_path',
    'db_backup_sha256','db_backup_size','db_backup_user_version','db_backup_manifest_id',
    'actual_windows_root','mount_read_write_checked','mount_read_only_now','safe_write_capability_proven',
    'rename_noreplace_capability_proven','mount_identity','free_bytes'}


def host_mount_identity(path, nas_source):
    # Bind the proof to the descriptor's actual mount, including remount/reboot
    # identity; a hidden older mountinfo row must never keep old proof valid.
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
    try:
        info=os.fstat(fd)
        ids=[line.split(':',1)[1].strip() for line in Path(f'/proc/self/fdinfo/{fd}').read_text().splitlines()
            if line.startswith('mnt_id:')]
        if len(ids)!=1 or not ids[0].isdigit():raise failure('RW_MOUNT_UNAVAILABLE')
        for line in Path('/proc/self/mountinfo').read_text().splitlines():
            before,_,after=line.partition(' - ')
            left,right=before.split(),after.split()
            if len(left)>=6 and len(right)>=3 and left[0]==ids[0] and left[4]==str(path):
                if (right[0]!='cifs' or right[1]!=nas_source or 'rw' not in left[5].split(',')
                        or 'rw' not in right[2].split(',') or os.fstatvfs(fd).f_flag & os.ST_RDONLY):
                    raise failure('RW_MOUNT_UNAVAILABLE')
                raw=hashlib.sha256(line.encode()).hexdigest()
                boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
                return f'{right[0]}:{right[1]}:{info.st_dev}:{info.st_ino}:{left[0]}:{raw}:{boot}'
        raise failure('RW_MOUNT_UNAVAILABLE')
    finally:os.close(fd)


def require_sha(value):
    if type(value) is not str or not SHA.fullmatch(value):
        raise ValueError('exact SHA-256 required')
    return value


def require_actor(value):
    if type(value) is not str or not value.strip() or len(value) > 128 or any(ord(c) < 32 for c in value):
        raise ValueError('explicit actor required')


class Conflict(ValueError):
    pass


class Forbidden(ValueError):
    pass


class Coordinator:
    def __init__(self, config, *, run=run_command, healthy=check_health):
        self.config, self.run, self.healthy = config, run, healthy
        self._runtime = {getattr(config,name): RuntimeDirectory(getattr(config,name),config.uid,config.gid)
            for name in RUNTIME_DIRECTORIES}
        self.active_source = None
        self.health_timeout = config.app_health_timeout_seconds
        self.app_restart_state = None
        self.app_stopped = False
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread = None
        self._cleanup_lock = threading.RLock()
        self._cleanup_cache = None
        self._cleanup_stopped = set()
        self._worker_stop_confirmed = False
        self._scanner_stopped = False
        self._writers_stopped = False
        self._snapshot_receipt = None
        self.metrics = {}
        secret = config.private_dir / 'csrf-secret.json'
        try:
            self.write_json(secret, {'key': secrets.token_hex(32)}, exclusive=True)
        except FileExistsError:
            pass
        self._key = bytes.fromhex(self.read_json(secret)['key'])
        if len(self._key) != 32:
            raise ValueError('invalid private CSRF key')

    def write_json(self, path, value, *, exclusive=False, mode=0o600, owner=None):
        runtime = self._runtime.get(path.parent)
        if runtime is not None:
            return runtime.write(path.name, value, exclusive=exclusive, mode=mode)
        return write_json(path, value, exclusive=exclusive, mode=mode, owner=owner)

    def read_json(self, path, limit=MAX_REQUEST):
        runtime = self._runtime.get(path.parent)
        return read_json(path, limit, fd=runtime.read(path.name) if runtime else None)

    def runtime_exists(self, path):
        try:
            os.stat(path.name, dir_fd=self._runtime[path.parent].fd, follow_symlinks=False)
            return True
        except FileNotFoundError:
            return False

    def check_runtime(self):
        for runtime in self._runtime.values():
            runtime.check()

    def compose(self, *args):
        deployment = (resolve_deployment(self.config.compose_files, approved_root=self.config.approved_root)
            if self.config.approved_root is not None else resolve_deployment(self.config.compose_files))
        if self.config.approved_root is not None:
            try:
                from reorganization_deployment import verify_active_release
            except ImportError:
                from deploy.nexus.reorganization_deployment import verify_active_release
            verify_active_release(deployment,self.config.compose_files,self.config.image)
        ensure_bindings(getattr(deployment,'bindings',()))
        argv = ['/usr/bin/docker', 'compose', '--project-directory', str(deployment.project_directory),
            '--env-file', str(deployment.env_file), '-p', deployment.project_name]
        for path in deployment.files:
            argv += ['-f', str(path)]
        return argv + list(args)

    def verify_image(self):
        image = self.run(self.compose('config', '--images', 'app')).strip().splitlines()
        if image != [self.config.image]:
            raise ValueError('Compose app image differs from fixed worker image')

    def container_name(self, request_id, kind, verb):
        return 'anime-db-v6-' + kind + '-' + request_id + '-' + verb

    def worker(self, verb, request_id, kind, *, receipt=None):
        self.check_runtime()
        c = self.config
        runtime_sources={}
        if c.approved_root is not None:
            root=c.private_dir/'worker-binds'
            root.mkdir(mode=0o700,exist_ok=True)
            for name in RUNTIME_DIRECTORIES:
                original=getattr(c,name)
                target=root/name
                if not target.exists():target.mkdir(mode=0o700)
                runtime=self._runtime[original]
                ensure_bindings([dict(source=str(original),target=str(target),dev=runtime.identity[0],ino=runtime.identity[1])])
                runtime_sources[original]=target
        self._worker_dispatched = False
        self._worker_stop_confirmed = False
        if verb in {'plan', 'publish', 'execute'}:
            self.write_stage_runtime(request_id, verb)
        self._worker_stop_confirmed = False
        argv = ['/usr/bin/docker', 'run', '--name', self.container_name(request_id, kind, verb),
            '--pull=never', '--network=none', '--read-only', '--init', '--cap-drop=ALL',
            '--security-opt=no-new-privileges', '--user', f'{c.uid}:{c.gid}',
            '--tmpfs', '/tmp:rw,noexec,nosuid,size=64m,mode=1777', '--entrypoint=python']
        for host, inside, ro in ((c.database_dir,'/data',False), (c.state_dir,'/state',False),
                (c.media_rw,'/media/anime',False), (c.inbox,'/approval-inbox',False),
                (c.outbox,'/approval-outbox',True), (c.ledger,'/approval-ledger',False),
                (c.backups,'/backups',False), (c.service_status,'/service-status',False),
                (c.worker_config,'/coordinator-config/worker.json',True)):
            argv += ['--mount', f'type=bind,src={runtime_sources.get(host,host)},dst={inside}' + (',readonly' if ro else '')]
        if receipt is not None:
            argv.append('--interactive')
        argv += [c.image, '-m', 'app.tools.reorganization_worker', '--config',
            '/coordinator-config/worker.json', verb, '--request-id', request_id]
        self._worker_dispatched = True
        try:
            return strict_json(self.run(argv, input=canonical(receipt).decode() if receipt else None, timeout=86400))
        except Exception:
            try:
                self.run(['/usr/bin/docker','stop','--time=10',self.container_name(request_id,kind,verb)])
                self._worker_stop_confirmed = True
            except Exception:
                # The private exception still records an unconfirmed stop. No
                # app restart or replacement worker is permitted for E7.
                pass
            raise

    def runtime_proof(self):
        if self.config.runtime_evidence is None:
            raise failure('CAPABILITIES_UNVERIFIED')
        try:
            proof=self.read_json(self.config.runtime_evidence)
            if (type(proof)!=dict or set(proof)!=RUNTIME_KEYS
                    or any(proof.get(key) is not True for key in
                        ('mount_read_write_checked','safe_write_capability_proven','rename_noreplace_capability_proven'))
                    or proof.get('mount_identity')!=host_mount_identity(self.config.media_rw,self.config.nas_source)):
                raise failure('CAPABILITIES_UNVERIFIED')
            return proof
        except (OSError,ValueError) as exc:
            if getattr(exc,'error_code',None):raise
            raise failure('CAPABILITIES_UNVERIFIED') from exc

    def write_stage_runtime(self, request_id, verb):
        proof=self.runtime_proof()
        if not self._scanner_stopped or not self._writers_stopped:
            raise failure('WRITERS_NOT_STOPPED')
        proof.update(scanner_paused_through_verification=self._scanner_stopped,
            inventory_writer_paused_through_verification=self._writers_stopped,mount_identity=None)
        if self._snapshot_receipt is not None:
            proof.update(self._snapshot_receipt)
            proof['snapshot_capable']=bool(self._snapshot_receipt.get('snapshot_id'))
        self.write_json(self.config.ledger/f'{request_id}.{verb}-runtime.json',proof,
            exclusive=True,owner=(self.config.uid,self.config.gid))

    def measured_worker(self, verb, request_id, kind, **kwargs):
        started=time.monotonic()
        try:return self.worker(verb,request_id,kind,**kwargs)
        finally:self.metrics[verb]=round(time.monotonic()-started,6)

    def valid_identity_result(self, result, data, expected_state):
        return (type(result)==dict and result.get('state')==expected_state and all(
            result.get(key)==data[key] for key in ('source_sha256','approved_sha256','manifest_id')))

    def status(self, request_id, kind, state, phase, detail, source=None, **extra):
        self.write_json(self.config.inbox / f'{request_id}.{kind}-status.json', dict(version=1,
            kind=kind, request_id=request_id, state=state, phase=phase, detail=detail, source_sha256=source, **extra),
            owner=(self.config.uid,self.config.gid))

    def progress(self, *, stage, detail, source=None, outcome='UNVERIFIED', active=True, app_ready=False, result_url=None,
                 app_restart_state=None):
        path = self.config.service_status / 'progress.json'
        data = None
        if source is not None:
            try:
                existing = self.read_json(path)
                if existing.get('source_sha256') == source:
                    data = existing
            except (OSError, ValueError):
                pass
        if data is None:
            data = dict(version=1, run_id=secrets.token_hex(16), completed=0, rename_completed=0,
                total=None, current_phase='manifest', waiting_for_operator=False, error=None,
                last_confirmed=None, phases=[dict(id=p,state='WORK' if p=='manifest' else 'WAIT') for p in PHASES])
        data.update(stage=stage,detail=detail,source_sha256=source,outcome=outcome,active=active,
            app_ready=app_ready,app_restart_state=app_restart_state,updated_at=time.time(),waiting_for_operator=False,
            result_url=result_url or ('/reorganization/approval/' + source if source else None))
        if outcome == 'STOP':
            for row in data['phases']:
                if row['state'] == 'WORK':
                    row['state'] = 'STOP'
        self.write_json(path,data,mode=0o644,owner=(self.config.uid,self.config.gid))

    def begin_heartbeat(self):
        self.end_heartbeat()
        self._heartbeat_stop.clear()
        path=self.config.service_status/'progress.json'
        run_id=self.read_json(path)['run_id']
        def beat():
            while not self._heartbeat_stop.wait(HEARTBEAT_SECONDS):
                try:
                    data=self.read_json(path)
                    if data.get('run_id')!=run_id or not data.get('active'):
                        return
                    data['updated_at']=time.time()
                    self.write_json(path,data,mode=0o644,owner=(self.config.uid,self.config.gid))
                except (OSError,ValueError):
                    return
        self._heartbeat_thread=threading.Thread(target=beat,daemon=True)
        self._heartbeat_thread.start()

    def end_heartbeat(self):
        self._heartbeat_stop.set()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join()
            self._heartbeat_thread=None

    def stop_writers(self):
        self.run(self.compose('stop','--timeout','30','app'))
        self.app_stopped = True
        self._scanner_stopped = not self.run(self.compose('ps','--status','running','--quiet','app')).strip()
        if not self._scanner_stopped:
            raise failure('EVIDENCE_UNCERTAIN')
        self._writers_stopped = strict_json(self.run([str(self.config.writers_stop_hook)])) == {'writers_stopped':True}
        if not self._writers_stopped:
            raise failure('EVIDENCE_UNCERTAIN')

    def _require_initializing_app(self, deadline):
        """An unavailable HTTP endpoint is pending only with live runtime proof."""
        def budget():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('app health wait reached maximum timeout')
            return min(10, remaining)
        container = self.run(self.compose('ps','--all','--quiet','app'), timeout=budget()).strip()
        if not re.fullmatch(r'[0-9a-f]{12,64}', container):
            raise ValueError('app container identity not confirmed')
        state = strict_json(self.run(['/usr/bin/docker','inspect','--format','{{json .State}}',container], timeout=budget()))
        if (type(state) is not dict or state.get('Status') not in {'created','running'}
                or state.get('Restarting') is not False or state.get('Dead') is not False
                or state.get('Paused') is not False or state.get('OOMKilled') is not False
                or state.get('Error') or (state['Status']=='running' and state.get('Running') is not True)):
            raise ValueError('app runtime failed during startup')

    def restart_app(self, *, source=None, outcome='UNVERIFIED'):
        self.app_restart_state = 'initializing'
        self.progress(stage='app_initializing',detail='Aplikace se inicializuje; čeká se na HTTP health a kontroluje se runtime.',
            source=source,outcome=outcome,app_restart_state=self.app_restart_state)
        self.begin_heartbeat()
        deadline = None
        try:
            self.verify_image()
            self.run(self.compose('up','-d','--no-deps','--no-build','--pull=never','app'))
            deadline = time.monotonic() + self.health_timeout
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError('app health wait reached maximum timeout')
                available = self.healthy(self.config.public_origin + '/_service/app-health')
                if time.monotonic() >= deadline:
                    raise TimeoutError('app health wait reached maximum timeout')
                if available:
                    break
                self._require_initializing_app(deadline)
                time.sleep(min(1, max(0, deadline - time.monotonic())))
            self.app_stopped = False
            self._scanner_stopped = False
            self._writers_stopped = False
            if strict_json(self.run([str(self.config.writers_start_hook)])) != {'writers_started':True}:
                raise ValueError('writer restart acknowledgement required')
            self.app_restart_state = 'available'
        except Exception as error:
            maximum_expired = (deadline is not None and time.monotonic() >= deadline
                and isinstance(error.__cause__, subprocess.TimeoutExpired))
            self.app_restart_state = 'timed_out' if isinstance(error, TimeoutError) or maximum_expired else 'failed'
            raise
        finally:
            self.end_heartbeat()

    def done(self, request_id, kind, state):
        self.write_json(self.config.private_dir / f'{request_id}.{kind}-done.json',
            dict(version=1,request_id=request_id,kind=kind,state=state),exclusive=True)

    def recover(self):
        for path in sorted(self.config.private_dir.glob('*-claim.json')):
            match = CLAIM.fullmatch(path.name)
            if not match:
                continue
            request_id,kind=match.groups()
            if (self.config.private_dir / f'{request_id}.{kind}-done.json').exists():
                continue
            # Fixed names derive only from canonical claim filenames. Stop an
            # orphan container before exposing STOP; never adopt or resume E7.
            for verb in (('execute',) if kind=='start' else ('plan','publish')):
                try:
                    self.run(['/usr/bin/docker','stop','--time=10',self.container_name(request_id,kind,verb)])
                except Exception:
                    pass
            source = request_id if kind=='start' else None
            self.status(request_id,kind,'STOP','interrupted','Hostitelská služba byla přerušena; správce musí ověřit zachované doklady.',source)
            self.progress(stage='interrupted',detail='Hostitelská služba byla přerušena; je nutná kontrola správce.',
                source=source,outcome='STOP',active=False)
            self.done(request_id,kind,'STOP')

    def validate_request(self, data, request_id, kind):
        common={'version','kind','request_id','actor','requested_at'}
        expected=common|({'preview_db_sha256'} if kind=='prepare' else {'source_sha256','approved_sha256','manifest_id'})
        if (type(data) is not dict or set(data)!=expected or type(data['version']) is not int
                or data['version']!=1 or data['kind']!=kind or data['request_id']!=request_id):
            raise ValueError('exact immutable request required')
        require_actor(data['actor'])
        at=datetime.fromisoformat(data['requested_at'].replace('Z','+00:00'))
        if at.tzinfo is None:
            raise ValueError('dated request required')
        age = (datetime.now(timezone.utc) - at).total_seconds()
        if not 0 <= age <= MAX_AGE_SECONDS:
            raise failure('REQUEST_EXPIRED')
        if kind=='prepare':
            require_sha(data['preview_db_sha256'])
        else:
            for name in ('source_sha256','approved_sha256','manifest_id'):
                require_sha(data[name])
            if data['source_sha256']!=request_id:
                raise ValueError('start request source differs')

    def process(self, path, request_id, kind):
        claim=self.config.private_dir / f'{request_id}.{kind}-claim.json'
        if claim.exists():return
        try:
            self.write_json(claim,dict(version=1,kind=kind,request_id=request_id),exclusive=True)
        except FileExistsError:return
        source=request_id if kind=='start' else None
        phase='request'
        self.app_stopped=False
        self._scanner_stopped=False
        self._writers_stopped=False
        self._snapshot_receipt=None
        self.app_restart_state=None
        self.metrics={}
        execution_attempted=False
        execution_returned=False
        data=None
        try:
            data=self.read_json(path)
            self.validate_request(data,request_id,kind)
            phase='mount'
            try:require_rw_mount(self.config.media_rw,self.config.nas_source)
            except (OSError,ValueError) as error:raise failure('RW_MOUNT_UNAVAILABLE') from error
            phase='capabilities'
            self.runtime_proof()
            phase='image'
            self.verify_image()
            if kind=='start':
                phase='start_preflight'
                checked=self.measured_worker('check-start',request_id,kind)
                if not self.valid_identity_result(checked,data,'READY'):
                    raise failure('APPROVAL_INVALID')
            phase='writers'
            self.status(request_id,kind,'RUNNING',phase,'Zastavují se nakonfigurovaní zapisovatelé.',source)
            self.progress(stage='preparing' if kind=='prepare' else 'execution',detail='Zastavují se nakonfigurovaní zapisovatelé.',source=source)
            self.begin_heartbeat()
            self.stop_writers()
            if kind=='prepare':
                phase='manifest'
                self.status(request_id,kind,'RUNNING',phase,'Vytváří se kanonický hostitelský manifest.',source)
                plan=self.measured_worker('plan',request_id,kind)
                if type(plan)!=dict or set(plan)!={'manifest_id','created_at','source_sha256'}:
                    raise ValueError('exact plan binding required')
                require_sha(plan['source_sha256']);require_sha(plan['manifest_id'])
                if datetime.fromisoformat(plan['created_at'].replace('Z','+00:00')).tzinfo is None:
                    raise ValueError('dated plan required')
                source=plan['source_sha256']
                phase='snapshot'
                self.status(request_id,kind,'RUNNING',phase,'Připravuje se NAS snapshot vázaný k manifestu.',source)
                started=time.monotonic()
                receipt=strict_json(self.run([str(self.config.snapshot_hook)],input=canonical(plan).decode(),timeout=600))
                self.metrics['snapshot']=round(time.monotonic()-started,6)
                if (type(receipt)!=dict or set(receipt)!={'snapshot_id','snapshot_at','snapshot_manifest_id'}
                        or receipt['snapshot_manifest_id']!=plan['manifest_id']
                        or type(receipt['snapshot_id'])!=str or not receipt['snapshot_id'].strip()
                        or datetime.fromisoformat(receipt['snapshot_at'].replace('Z','+00:00')).tzinfo is None):
                    raise failure('SNAPSHOT_FAILED')
                self._snapshot_receipt=receipt
                self.write_json(self.config.private_dir/f'{request_id}.snapshot.json',receipt,exclusive=True)
                phase='publish'
                publication=self.measured_worker('publish',request_id,kind,receipt=receipt)
                if publication!={'source_sha256':source}:
                    raise ValueError('published source differs from planned snapshot binding')
                phase='app_restart'
                self.restart_app(source=source)
                self.end_heartbeat()
                self.status(request_id,kind,'READY','ready','Manifest je připraven k přesné kontrole v prohlížeči.',source,
                    app_restart_state=self.app_restart_state,
                    timings_seconds=dict(self.metrics))
                self.progress(stage='ready',detail='Manifest je připraven k přesné kontrole v prohlížeči.',source=source,
                    active=False,app_ready=True,app_restart_state=self.app_restart_state)
                self.done(request_id,kind,'READY')
            else:
                phase='execution'
                self.end_heartbeat()
                self.active_source=source
                execution_attempted=True
                result=self.measured_worker('execute',request_id,kind)
                execution_returned=True
                original=self.read_json(self.config.inbox/(source+'.result.json'),MAX_PROPOSAL)
                if not all(self.valid_identity_result(row,data,'NORMAL') for row in (result,original)):
                    raise failure('EVIDENCE_UNCERTAIN')
                self.active_source=None
                phase='app_restart'
                try:self.restart_app(source=source,outcome='NORMAL')
                except Exception:
                    detail = ('E7 skončilo NORMAL; vypršel maximální čas pro dostupnost aplikace.'
                        if self.app_restart_state=='timed_out' else 'E7 skončilo NORMAL; restart aplikace selhal.')
                    self.status(request_id,kind,'NORMAL','app_restart_failed',detail,source,
                        app_restart_state=self.app_restart_state,
                        timings_seconds=dict(self.metrics))
                    self.progress(stage='app_restart_failed',detail=detail,source=source,
                        outcome='NORMAL',active=False,app_restart_state=self.app_restart_state)
                else:
                    self.status(request_id,kind,'NORMAL','normal','E7 skončilo NORMAL a dostupnost aplikace je ověřená.',source,
                        app_restart_state=self.app_restart_state,
                        timings_seconds=dict(self.metrics))
                    self.progress(stage='normal',detail='E7 skončilo NORMAL a dostupnost aplikace je ověřená.',source=source,
                        outcome='NORMAL',active=False,app_ready=True,app_restart_state=self.app_restart_state)
                self.done(request_id,kind,'NORMAL')
        except Exception as error:
            self.end_heartbeat()
            self.active_source=None
            fallback={'request':'APPROVAL_INVALID','mount':'RW_MOUNT_UNAVAILABLE','capabilities':'CAPABILITIES_UNVERIFIED',
                'snapshot':'SNAPSHOT_FAILED','manifest':'PLAN_UNRESOLVED','writers':'WRITERS_NOT_STOPPED',
                'start_preflight':'APPROVAL_INVALID'}.get(phase,'WORKER_FAILED')
            code=diagnostic_code(error,fallback)
            if phase=='app_restart':
                code='APP_RESTART_TIMED_OUT' if self.app_restart_state=='timed_out' else 'APP_RESTART_FAILED'
            if code not in PUBLIC_ERRORS:code=fallback
            detail=PUBLIC_ERRORS[code]
            restored=False
            e7_started=None if kind=='start' else False
            # The normal app returns only after a positive original idle/guard
            # check. A missing execution response or spent E7 claim is uncertain.
            safe_before_execution=kind=='prepare'
            if kind=='start' and not execution_returned and data is not None:
                if not self.app_stopped and not execution_attempted:
                    safe_before_execution=True
                elif not execution_attempted or not self._worker_dispatched or self._worker_stop_confirmed:
                    try:
                        idle=self.measured_worker('check-idle',request_id,kind)
                        safe_before_execution=self.valid_identity_result(idle,data,'E7_NOT_STARTED')
                    except Exception:safe_before_execution=False
                if safe_before_execution:e7_started=False
            if self.app_stopped and safe_before_execution and phase!='app_restart':
                try:self.restart_app(source=source);restored=True
                except Exception:detail+=' Restart aplikace přes startup guard nebyl ověřen.'
            elif safe_before_execution and not self.app_stopped:
                restored=self.healthy(self.config.public_origin+'/_service/app-health')
            if kind=='start' and e7_started is False:
                phase='execution_not_started'
                detail='Příprava spuštění selhala, E7 nezačalo. '+detail
            private=dict(phase=phase,error=type(error).__name__,detail=str(error)[:MAX_DIAGNOSTIC],
                stderr=getattr(error,'stderr','')[-MAX_DIAGNOSTIC:],stdout=getattr(error,'stdout','')[-MAX_DIAGNOSTIC:],
                returncode=getattr(error,'returncode',None))
            try:self.write_json(self.config.private_dir/f'{request_id}.{kind}-error.json',private,exclusive=True)
            finally:
                self.status(request_id,kind,'STOP',phase,detail,source,error_code=code,e7_started=e7_started,
                    app_restart_state=self.app_restart_state,
                    timings_seconds=dict(self.metrics))
                result_url=('/reorganization/jobs/'+request_id if kind=='prepare' else '/reorganization/approval/'+request_id)
                self.progress(stage='execution_not_started' if kind=='start' and e7_started is False else 'stopped',
                    detail=detail,source=source,outcome='STOP',active=False,app_ready=restored,result_url=result_url,
                    app_restart_state=self.app_restart_state)
                self.done(request_id,kind,'STOP')
        finally:
            self.end_heartbeat()
            self.active_source=None
            self.write_json(self.config.private_dir/f'{request_id}.{kind}-timing.json',dict(self.metrics),exclusive=True)

    def tick(self):
        self.check_runtime()
        for name in sorted(os.listdir(self._runtime[self.config.outbox].fd)):
            path = self.config.outbox/name
            match=REQUEST.fullmatch(path.name)
            if match:
                self.process(path,*match.groups())

    def stop_cleanup(self, source):
        self._cleanup_stopped.add(source)
        marker=self.config.ledger/(source+'.cleanup-stop.json')
        try:
            self.write_json(marker,dict(version=1,source_sha256=source,reason='EVIDENCE_UNCERTAIN'),
                exclusive=True,owner=(self.config.uid,self.config.gid))
        except FileExistsError:pass
        except OSError:
            # If even the stop marker cannot be published, stop the fixed worker
            # rather than leave it waiting forever for a missing decision file.
            self.run(['/usr/bin/docker','stop','--time=10',self.container_name(source,'start','execute')])

    def cleanup_current(self):
        source=self.active_source
        if source is None:return None
        path=self.config.ledger/(source+'.cleanup-request.json')
        try:
            info=os.stat(path.name,dir_fd=self._runtime[path.parent].fd,follow_symlinks=False)
        except FileNotFoundError:return None
        key=(source,info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)
        if self._cleanup_cache is not None and self._cleanup_cache[0]==key:
            return self._cleanup_cache[1]
        try:
            record=self.read_json(path,MAX_PROPOSAL)
            if (type(record)!=dict or set(record)!={'source_sha256','approved_sha256','proposal'}
                    or record['source_sha256']!=source):
                raise ValueError('invalid current cleanup request')
            require_sha(record['approved_sha256'])
            proposal=record['proposal']
            if (type(proposal)!=dict or proposal.get('proposal_sha256')!=hashlib.sha256(canonical(
                    {k:v for k,v in proposal.items() if k!='proposal_sha256'})).hexdigest()
                    or type(proposal.get('candidates'))!=list
                    or any(type(row)!=dict or type(row.get('path'))!=str or type(row.get('disposition'))!=str
                        for row in proposal['candidates'])):
                raise ValueError('cleanup proposal seal differs')
            self._cleanup_cache=(key,record)
            return record
        except (OSError,ValueError,KeyError,TypeError):
            self.stop_cleanup(source)
            raise

    def cleanup_decision_state(self, source):
        if source in self._cleanup_stopped or self.runtime_exists(self.config.ledger/(source+'.cleanup-stop.json')):return 'STOP'
        if self.runtime_exists(self.config.ledger/(source+'.cleanup-decision.json')):return 'CONSUMED'
        if (self.config.private_dir/(source+'.cleanup-claim.json')).exists():
            self.stop_cleanup(source)
            return 'STOP'
        return 'WAITING'

    def cleanup_token(self, record, cookie):
        binding=[record['source_sha256'],record['approved_sha256'],record['proposal']['proposal_sha256'],cookie]
        return hmac.new(self._key,canonical(binding),hashlib.sha256).hexdigest()

    def cleanup_view(self, cookie):
        with self._cleanup_lock:
            record=self.cleanup_current()
            if record is None:return {'waiting':False},None
            if type(cookie)!=str or not SHA.fullmatch(cookie):cookie=secrets.token_hex(32)
            state=self.cleanup_decision_state(record['source_sha256'])
            return dict(waiting=state=='WAITING',source_sha256=record['source_sha256'],
                approved_sha256=record['approved_sha256'],proposal_sha256=record['proposal']['proposal_sha256'],
                candidate_count=len(record['proposal']['candidates']),decision_state=state,
                phase='cleanup' if state=='WAITING' else 'cleanup_decision' if state=='CONSUMED' else 'cleanup_evidence_stop'),cookie

    def cleanup_proposal(self, cookie, proposal_sha256):
        with self._cleanup_lock:
            record=self.cleanup_current()
            if (record is None or proposal_sha256!=record['proposal']['proposal_sha256']
                    or self.cleanup_decision_state(record['source_sha256'])!='WAITING'):
                raise Conflict('cleanup proposal changed or consumed')
            if type(cookie)!=str or not SHA.fullmatch(cookie):cookie=secrets.token_hex(32)
            return dict(waiting=True,source_sha256=record['source_sha256'],approved_sha256=record['approved_sha256'],
                proposal=dict(proposal_sha256=proposal_sha256,candidates=record['proposal']['candidates']),
                csrf=self.cleanup_token(record,cookie)),cookie

    def cleanup_decide(self, data, *, origin, cookie):
        with self._cleanup_lock:
            if origin!=self.config.public_origin or type(cookie)!=str or not SHA.fullmatch(cookie):
                raise Forbidden('same Origin and session cookie required')
            record=self.cleanup_current()
            if record is None or self.cleanup_decision_state(record['source_sha256'])!='WAITING':
                raise Conflict('no live cleanup decision')
            expected={'source_sha256','approved_sha256','proposal_sha256','actor','directories','confirm','csrf'}
            if type(data)!=dict or set(data) not in (expected,expected|{'decision'}):
                raise ValueError('exact cleanup decision required')
            if any(data[key]!=record[key] for key in ('source_sha256','approved_sha256')) or data['proposal_sha256']!=record['proposal']['proposal_sha256']:
                raise Conflict('cleanup proposal changed')
            if type(data['csrf'])!=str or not hmac.compare_digest(data['csrf'],self.cleanup_token(record,cookie)):
                raise Forbidden('proposal bound CSRF required')
            require_actor(data['actor'])
            selected=data['directories']
            if (data['confirm'] is not True or type(selected)!=list or any(type(p)!=str for p in selected)
                    or len(selected)!=len(set(selected)) or data.get('decision') not in (None,'stop')):
                raise ValueError('explicit exact cleanup selection required')
            if data.get('decision')=='stop' and selected:
                raise ValueError('STOP cannot select directories')
            candidates={row['path'] for row in record['proposal']['candidates']}
            if not set(selected)<=candidates:
                raise ValueError('only exact candidate paths may be selected')
            for path in selected:
                parts=path.split('/')
                if (any(p in ('','.','..') for p in parts) or '\\' in path
                        or parts[0].casefold() in ('import','duplicates','#recycle','@eadir')
                        or any(p.casefold() in ('#recycle','@eadir') for p in parts)):
                    raise ValueError('protected cleanup path')
            for entry in record['proposal']['before_snapshot']:
                parts=entry['path'].split('/')
                if any('/'.join(parts[:depth]) in selected for depth in range(1,len(parts))) and (
                        entry['kind']!='directory' or entry['path'] not in selected):
                    raise ValueError('approved directory has retained children')
            decision={key:value for key,value in data.items() if key!='csrf'}
            # Host-private one-use claim survives any uncertainty publishing the
            # worker-visible decision; a second POST can never become a retry.
            try:
                self.write_json(self.config.private_dir/(record['source_sha256']+'.cleanup-claim.json'),
                    decision,exclusive=True)
            except FileExistsError as exc:
                raise Conflict('cleanup decision already consumed') from exc
            try:
                self.write_json(self.config.ledger/(record['source_sha256']+'.cleanup-decision.json'),decision,
                    exclusive=True,owner=(self.config.uid,self.config.gid))
            except OSError:
                self.stop_cleanup(record['source_sha256'])
                raise


class ControlServer(socketserver.ThreadingMixIn,socketserver.UnixStreamServer):
    daemon_threads=True
    allow_reuse_address=False


def make_control_server(coordinator):
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def setup(self):
            self.request.settimeout(5)
            super().setup()
        def log_message(self,*args):
            pass
        def response(self,status,value,cookie=None):
            raw=canonical(value)
            self.send_response(status)
            self.send_header('Content-Type','application/json')
            self.send_header('Cache-Control','no-store')
            self.send_header('Content-Length',str(len(raw)))
            self.send_header('Connection','close')
            if cookie:
                suffix='; Secure' if coordinator.config.public_origin.startswith('https://') else ''
                self.send_header('Set-Cookie',f'{COOKIE}={cookie}; Path=/_service/control/; HttpOnly; SameSite=Strict'+suffix)
            self.end_headers();self.wfile.write(raw)
            self.close_connection=True
        def cookie(self):
            values=SimpleCookie()
            try:
                values.load(self.headers.get('Cookie',''))
                return values[COOKIE].value if COOKIE in values else None
            except Exception:
                return None
        def do_GET(self):
            parsed=urlsplit(self.path)
            if parsed.path not in {'/cleanup','/cleanup/proposal'}:
                return self.response(404,{'error':'Neznámý endpoint.'})
            try:
                if parsed.path=='/cleanup':
                    if parsed.query:raise ValueError('status query rejected')
                    view,cookie=coordinator.cleanup_view(self.cookie())
                else:
                    query=parse_qs(parsed.query,strict_parsing=True)
                    if set(query)!={'sha'} or len(query['sha'])!=1:raise ValueError('exact proposal query required')
                    require_sha(query['sha'][0])
                    view,cookie=coordinator.cleanup_proposal(self.cookie(),query['sha'][0])
                self.response(200,view,cookie)
            except Conflict:
                self.response(409,{'error':'Návrh úklidu se změnil nebo rozhodnutí již bylo spotřebováno.'})
            except (ValueError,OSError):
                self.response(409,{'error':'Aktuální návrh úklidu není dostupný; stav vyžaduje STOP.'})
        def do_POST(self):
            if self.path!='/cleanup':
                return self.response(404,{'error':'Neznámý endpoint.'})
            try:
                length=self.headers.get('Content-Length')
                if (self.headers.get('Transfer-Encoding') or length is None or not length.isdecimal()
                        or not 0<int(length)<=MAX_DECISION or len(self.headers.get_all('Content-Length'))!=1
                        or self.headers.get_content_type()!='application/json'):
                    return self.response(400,{'error':'Je vyžadováno omezené JSON tělo požadavku.'})
                raw=self.rfile.read(int(length))
                if len(raw)!=int(length):
                    raise ValueError('incomplete request')
                coordinator.cleanup_decide(strict_json(raw),origin=self.headers.get('Origin'),cookie=self.cookie())
                self.response(200,{'accepted':True})
            except Forbidden:
                self.response(403,{'error':'Je vyžadován stejný Origin a CSRF vázané k návrhu.'})
            except Conflict:
                self.response(409,{'error':'Návrh úklidu se změnil nebo rozhodnutí již bylo spotřebováno.'})
            except (ValueError,OSError):
                self.response(400,{'error':'Neplatné rozhodnutí o úklidu.'})
    path=coordinator.config.control_dir/'coordinator.sock'
    if path.exists():
        if not stat.S_ISSOCK(path.lstat().st_mode):
            raise ValueError('control path is not a socket')
        path.unlink()
    server=ControlServer(str(path),Handler)
    os.chmod(path,0o660)
    if os.geteuid()==0:
        os.chown(path,0,coordinator.config.socket_gid)
    return server


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    args=parser.parse_args()
    if os.geteuid()!=0:
        parser.error('host root service required')
    config=load_config(args.config)
    lock=os.open(config.private_dir/'coordinator.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    coordinator=Coordinator(config)
    coordinator.recover()
    server=make_control_server(coordinator)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    try:
        while True:
            coordinator.tick()
            time.sleep(1)
    finally:
        server.shutdown();server.server_close();os.close(lock)


if __name__=='__main__':
    main()
