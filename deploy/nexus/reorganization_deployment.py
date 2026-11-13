#!/usr/bin/env python3
"""Approve exact Compose input copies for the existing AnimeDB deployment.

The source checkout stays user-owned. Only a separately reviewed root snapshot
is supplied to privileged Compose. This command never runs Docker or changes data.
"""
from dataclasses import dataclass
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat

try:
    from host_io import open_file, open_directory, ensure_bindings
except ImportError:
    from deploy.nexus.host_io import open_file, open_directory, ensure_bindings

MAX_BYTES = 2 * 1024 * 1024
NAMES = ('compose.1.yaml', 'compose.2.yaml', 'compose.3.yaml')
INDIRECT = re.compile(r'(?:^|[\s,{])(?:[\'\"])?(?:include|extends|env_file|label_file|secrets|configs)(?:[\'\"])?\s*:', re.M)
ANCHOR = re.compile(r'(?:^|[\s\[{,:])[&*][A-Za-z0-9_-]+', re.M)


@dataclass(frozen=True)
class Deployment:
    files: tuple[Path, Path, Path]
    env_file: Path
    project_directory: Path
    project_name: str = 'anime-db'
    bindings: tuple = ()


def _require_root():
    if os.geteuid() != 0:
        raise ValueError('root approval command required')


def _absolute(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts or any(c in str(path) for c in ('\x00', '\n', '\r', ',')):
        raise ValueError('canonical absolute deployment path required')
    return path


def _trusted_path(path, *, directory=False):
    path = _absolute(path)
    for component in (path, *path.parents):
        info = component.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError('approved deployment must be root controlled')
    if directory and not path.is_dir() or not directory and not path.is_file():
        raise ValueError('invalid approved deployment file type')


def _read(path):
    path = _absolute(path)
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValueError('deployment symlinks are not accepted')
    fd = open_file(path)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > MAX_BYTES:
            raise ValueError('independent bounded deployment input required')
        data = os.pread(fd, before.st_size + 1, 0)
        after = os.fstat(fd)
        if len(data) != before.st_size or (before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns) != (
                after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns):
            raise ValueError('deployment input changed during copy')
        return data
    finally:
        os.close(fd)


def _canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',', ':'),ensure_ascii=True,allow_nan=False).encode()


def _json(raw):
    def pairs(values):
        result = {}
        for key,value in values:
            if key in result: raise ValueError('duplicate deployment field')
            result[key] = value
        return result
    return json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite JSON')))


def _write(path, raw, *, replace=False):
    temp = path.with_name('.'+path.name+'.'+secrets.token_hex(12))
    fd = os.open(temp,os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,0o600)
    try:
        with os.fdopen(fd,'wb',closefd=False) as stream:
            stream.write(raw); stream.flush(); os.fsync(fd)
        os.fchmod(fd,0o444)
        if not replace and path.exists(): raise FileExistsError(path)
        os.replace(temp,path)
        parent = os.open(path.parent,os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try: os.fsync(parent)
        finally: os.close(parent)
    finally:
        os.close(fd)
        if temp.exists(): temp.unlink()


def _sources(compose_files):
    if type(compose_files) not in (tuple,list) or len(compose_files) != 3:
        raise ValueError('exact three Compose sources in base/edge/V6 order required')
    files = tuple(_absolute(p) for p in compose_files)
    if len(set(files)) != 3: raise ValueError('distinct Compose sources required')
    return files


def _bind_sources(text):
    """Conservative supported block-volume grammar, not a general YAML parser.

    Reject short syntax (implicit host mkdir), flow lists and indentationless
    sequences. Every accepted bind explicitly disables host path creation.
    """
    sources, volume_indent, item = [], None, None
    def finish():
        if item is not None and set(item) != {'type','source','target','create_host_path'}:
            raise ValueError('absolute long-form bind with create_host_path: false required')
    for number,line in enumerate(text.splitlines(keepends=True)):
        raw_field = re.match(r'\s*(source|target)\s*:\s*(.*)',line)
        if raw_field:
            literal = raw_field[2].strip()
            if literal.startswith(('"', "'")):
                quoted = re.fullmatch(r"(['\"])(.*?)\1(?:\s+#.*)?",literal)
                if quoted is None: raise ValueError('unsupported quoted bind path')
                literal = quoted[2]
            else:
                literal = re.split(r'\s+#',literal,maxsplit=1)[0]
            if '#' in literal:
                raise ValueError('# in bind source/target is unsupported')
        clean = re.split(r'\s+#',line,maxsplit=1)[0].strip()
        if not clean or clean.startswith('#'): continue
        indent = len(line)-len(line.lstrip())
        if volume_indent is not None and indent <= volume_indent:
            if clean.startswith('-'):
                raise ValueError('absolute binds require indented block sequences')
            finish(); item=None; volume_indent=None
        if re.match(r'volumes\s*:',clean):
            if clean != 'volumes:':
                raise ValueError('absolute binds require block volumes')
            volume_indent=indent
        elif volume_indent is not None:
            if clean.startswith('-'):
                finish(); item={}
                if clean != '- type: bind':
                    raise ValueError('absolute long-form bind required; no implicit host creation')
                item['type']='bind'
            elif item is not None:
                key,sep,value=clean.partition(':')
                value=value.strip().strip('"\'')
                if key in ('source','target','create_host_path'):
                    if key in item: raise ValueError('duplicate bind field')
                    item[key]=value
                    if key in ('source','target') and (not value.startswith('/') or '..' in Path(value).parts or '$' in value):
                        raise ValueError('absolute literal bind source/target required')
                    if key=='source':sources.append((number,value))
                    if key=='create_host_path' and value!='false':
                        raise ValueError('absolute bind must disable host creation')
                elif key not in ('read_only','bind','propagation'):
                    raise ValueError('unsupported bind field')
            else:
                raise ValueError('absolute block bind item required')
    finish()
    return sources


def _pin_sources(raws,generation):
    bindings=[]; mapped={}; result=[]
    for raw in raws:
        lines=raw.decode().splitlines(keepends=True)
        for number,source in _bind_sources(raw.decode()):
            try:
                _trusted_path(Path(source).parent,directory=True)
                fd=open_directory(source)
                os.close(fd)
                continue
            except ValueError:
                pass
            if source not in mapped:
                fd=open_directory(source)
                try: info=os.fstat(fd)
                finally: os.close(fd)
                target=generation/'binds'/('bind-'+str(len(bindings)))
                if not target.parent.exists():target.parent.mkdir(mode=0o700)
                target.mkdir(mode=0o700)
                bindings.append(dict(source=source,target=str(target),dev=info.st_dev,ino=info.st_ino))
                mapped[source]=str(target)
            prefix=lines[number].split('source:',1)[0]
            lines[number]=prefix+'source: '+mapped[source]+'\n'
        result.append(''.join(lines).encode())
    return result,bindings


def _check_inputs(raws, env, *, absolute_binds=False):
    for raw in raws:
        text = raw.decode('utf-8')
        if absolute_binds:
            _bind_sources(text)
        # The supported production three-file stack is block YAML. Conservatively
        # reject additional configuration readers instead of following live files.
        if '\\' in text or INDIRECT.search(text) or ANCHOR.search(text):
            raise ValueError('external or escaped Compose inputs require explicit integration review')
        if any(line.lstrip().startswith(('{','[')) for line in text.splitlines()):
            raise ValueError('block Compose YAML required for the approved input contract')
    text = env.decode('utf-8')
    for line in text.splitlines():
        value = line.strip()
        if not value or value.startswith('#'): continue
        if not re.fullmatch(r'ANIMEDB_(?:UID|GID|CONTROL_GID)=[1-9][0-9]*',value):
            raise ValueError('only numeric ANIMEDB_UID/ANIMEDB_GID/ANIMEDB_CONTROL_GID interpolation is supported')


def stage_deployment(compose_files, *, env_file=None, project_name='anime-db', approved_root=None):
    _require_root()
    files = _sources(compose_files)
    if project_name not in {'anime-db','animedb-v6-lab'}:
        raise ValueError('explicit AnimeDB production or isolated lab project required')
    env_file = _absolute(env_file or files[0].parent/'.env')
    raws,env = [_read(path) for path in files],_read(env_file)
    _check_inputs(raws,env,absolute_binds=approved_root is not None)
    root = _absolute(approved_root) if approved_root is not None else files[0].parent/'approved-v6'
    if not root.exists():
        # Only the new evidence root needs trusted ancestors, not the source stack.
        _trusted_path(root.parent,directory=True)
        root.mkdir(mode=0o700)
    _trusted_path(root,directory=True)
    generation = root/('generation-'+secrets.token_hex(12)); generation.mkdir(mode=0o700)
    original_hashes=[hashlib.sha256(raw).hexdigest() for raw in raws]
    bindings=[]
    if approved_root is not None:
        raws,bindings=_pin_sources(raws,generation)
    rows = []
    for name,data in zip(NAMES,raws):
        _write(generation/name,data)
        rows.append(dict(name=name,sha256=hashlib.sha256(data).hexdigest()))
    _write(generation/'.env',env)
    metadata = dict(version=1,sources=[str(p) for p in files],env_source=str(env_file),
        project_directory=str(files[0].parent),project_name=project_name,files=rows,
        environment=dict(name='.env',sha256=hashlib.sha256(env).hexdigest()))
    if approved_root is not None:
        metadata.update(version=2,source_sha256=original_hashes,bindings=bindings)
    _write(generation/'deployment.json',_canonical(metadata))
    return generation


def generation_sha256(generation):
    path = _absolute(generation)/'deployment.json'
    _trusted_path(path)
    return hashlib.sha256(_read(path)).hexdigest()


def _load_generation(generation, expected_sha=None):
    generation = _absolute(generation)
    if not re.fullmatch(r'generation-[0-9a-f]{24}',generation.name):
        raise ValueError('approved generation name required')
    _trusted_path(generation,directory=True)
    raw = _read(generation/'deployment.json'); _trusted_path(generation/'deployment.json')
    if expected_sha is not None and hashlib.sha256(raw).hexdigest() != expected_sha:
        raise ValueError('reviewed deployment manifest changed')
    value = _json(raw)
    base={'version','sources','env_source','project_directory','project_name','files','environment'}
    if type(value) is not dict or value.get('version') not in (1,2) or set(value) != (base if value['version']==1 else base|{'source_sha256','bindings'}):
        raise ValueError('invalid deployment metadata')
    sources = _sources(value['sources'])
    if (value['project_directory'] != str(sources[0].parent)
            or value['project_name'] not in {'anime-db','animedb-v6-lab'}
            or type(value['files']) is not list or len(value['files']) != 3):
        raise ValueError('deployment context differs')
    rows = value['files']+[value['environment']]
    raws=[]
    for name,row in zip((*NAMES,'.env'),rows):
        if type(row) is not dict or set(row) != {'name','sha256'} or row['name'] != name:
            raise ValueError('deployment input order differs')
        path = generation/name; _trusted_path(path)
        data = _read(path)
        if hashlib.sha256(data).hexdigest() != row['sha256']:
            raise ValueError('approved deployment bytes changed')
        raws.append(data)
    _check_inputs(raws[:3],raws[3],absolute_binds=generation.parent != sources[0].parent/'approved-v6')
    bindings=value.get('bindings',[])
    if type(bindings) is not list:
        raise ValueError('invalid binding metadata')
    for i,row in enumerate(bindings):
        if (type(row) is not dict or set(row)!={'source','target','dev','ino'}
                or row['target']!=str(generation/'binds'/('bind-'+str(i)))
                or type(row['dev']) is not int or type(row['ino']) is not int):
            raise ValueError('invalid pinned binding')
        _absolute(row['source'])
        _trusted_path(Path(row['target']).parent,directory=True)
    if value['version']==2 and (type(value['source_sha256']) is not list or len(value['source_sha256'])!=3
            or any(not isinstance(h,str) or not re.fullmatch('[0-9a-f]{64}',h) for h in value['source_sha256'])):
        raise ValueError('invalid original source fingerprints')
    return value,Deployment(tuple(generation/name for name in NAMES),generation/'.env',sources[0].parent,value['project_name'],tuple(bindings))


def approve_deployment(generation, *, reviewed_sha256):
    _require_root()
    if not re.fullmatch('[0-9a-f]{64}',reviewed_sha256): raise ValueError('exact reviewed SHA required')
    generation = _absolute(generation)
    _load_generation(generation,reviewed_sha256)
    current = generation.parent/'current.json'
    if current.exists(): _trusted_path(current)
    _write(current,_canonical(dict(version=1,generation=generation.name,manifest_sha256=reviewed_sha256)),replace=True)
    return current


def resolve_deployment(compose_files, *, approved_root=None):
    files = _sources(compose_files)
    root = _absolute(approved_root) if approved_root is not None else files[0].parent/'approved-v6'
    current = root/'current.json'; _trusted_path(current)
    value = _json(_read(current))
    if (type(value) is not dict or set(value) != {'version','generation','manifest_sha256'} or value['version'] != 1
            or type(value['generation']) is not str or not re.fullmatch(r'generation-[0-9a-f]{24}',value['generation'])
            or type(value['manifest_sha256']) is not str or not re.fullmatch('[0-9a-f]{64}',value['manifest_sha256'])):
        raise ValueError('invalid approved deployment selection')
    metadata,deployment = _load_generation(current.parent/value['generation'],value['manifest_sha256'])
    if metadata['sources'] != [str(p) for p in files]:
        raise ValueError('approved three-source deployment differs from configuration')
    return deployment



def active_commit(src):
    # Read Git records as data. Never execute Git or hooks from a user checkout
    # with root authority. Ordinary checkout required; no worktree indirection.
    git = src/'.git'
    head = _read(git/'HEAD').decode().strip()
    if head.startswith('ref: '):
        ref = head[5:]
        if not re.fullmatch(r'refs/heads/[A-Za-z0-9_./-]+',ref) or '..' in ref.split('/'):
            raise ValueError('invalid active source ref')
        try:
            head = _read(git/ref).decode().strip()
        except FileNotFoundError:
            entries = [line.split() for line in _read(git/'packed-refs').decode().splitlines()
                if line and not line.startswith(('#','^'))]
            matches = [sha for sha,name in entries if name == ref]
            if len(matches) != 1: raise ValueError('active source ref unavailable')
            head = matches[0]
    if not re.fullmatch('[0-9a-f]{40}',head):
        raise ValueError('full active source commit required')
    return head


def verify_active_release(deployment,files,image):
    suffix = Path('deploy/nexus/service-edge/compose.edge.yaml')
    if tuple(files[1].parts[-4:]) != suffix.parts or files[1].parents[3].name != 'src':
        raise ValueError('edge source must belong to the active src release')
    src = files[1].parents[3]
    release = active_commit(src)
    tag = re.fullmatch(r'.+:v6-([0-9a-f]{7,40})',image)
    if tag is None or not release.startswith(tag[1]):
        raise ValueError('approved image differs from active src release')
    # No silent boot with an obsolete approved generation after a release swap.
    metadata = _json(_read(deployment.files[0].parent/'deployment.json'))
    expected=metadata.get('source_sha256')
    if (any(hashlib.sha256(_read(live)).hexdigest()!=sha for live,sha in zip(files,expected)) if expected is not None
            else any(_read(live) != _read(approved) for live,approved in zip(files,deployment.files))):
        raise ValueError('active Compose sources differ from approved generation')
    if _read(Path(metadata['env_source'])) != _read(deployment.env_file):
        raise ValueError('active deployment environment differs from approval')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    stage=sub.add_parser('stage'); stage.add_argument('--compose',type=Path,action='append',required=True)
    stage.add_argument('--approved-root',type=Path); stage.add_argument('--env-file',type=Path); stage.add_argument('--project-name',default='anime-db',choices=('anime-db','animedb-v6-lab'))
    approve=sub.add_parser('approve'); approve.add_argument('--generation',type=Path,required=True); approve.add_argument('--reviewed-sha256',required=True)
    args=parser.parse_args(argv)
    try:
        if args.command == 'stage':
            path=stage_deployment(args.compose,env_file=args.env_file,project_name=args.project_name,approved_root=args.approved_root)
            print(json.dumps(dict(generation=str(path),reviewed_sha256=generation_sha256(path))))
        else:
            print(json.dumps(dict(current=str(approve_deployment(args.generation,reviewed_sha256=args.reviewed_sha256)))))
        return 0
    except (ValueError,OSError) as exc:
        print('STOP: '+str(exc),file=__import__('sys').stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
