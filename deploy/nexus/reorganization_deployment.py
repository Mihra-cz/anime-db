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
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
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


def _check_inputs(raws, env):
    for raw in raws:
        text = raw.decode('utf-8')
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
        if not re.fullmatch(r'ANIMEDB_(?:UID|GID)=[1-9][0-9]*',value):
            raise ValueError('only numeric ANIMEDB_UID/ANIMEDB_GID interpolation is supported')


def stage_deployment(compose_files, *, env_file=None, project_name='anime-db'):
    _require_root()
    files = _sources(compose_files)
    if project_name not in {'anime-db','animedb-v6-lab'}:
        raise ValueError('explicit AnimeDB production or isolated lab project required')
    env_file = _absolute(env_file or files[0].parent/'.env')
    raws,env = [_read(path) for path in files],_read(env_file)
    _check_inputs(raws,env)
    root = files[0].parent/'approved-v6'
    if not root.exists():
        # The first Compose belongs to the existing root-controlled stack dir.
        _trusted_path(files[0].parent,directory=True)
        root.mkdir(mode=0o700)
    _trusted_path(root,directory=True)
    generation = root/('generation-'+secrets.token_hex(12)); generation.mkdir(mode=0o700)
    rows = []
    for name,data in zip(NAMES,raws):
        _write(generation/name,data)
        rows.append(dict(name=name,sha256=hashlib.sha256(data).hexdigest()))
    _write(generation/'.env',env)
    metadata = dict(version=1,sources=[str(p) for p in files],env_source=str(env_file),
        project_directory=str(files[0].parent),project_name=project_name,files=rows,
        environment=dict(name='.env',sha256=hashlib.sha256(env).hexdigest()))
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
    if type(value) is not dict or set(value) != {'version','sources','env_source','project_directory','project_name','files','environment'} or value['version'] != 1:
        raise ValueError('invalid deployment metadata')
    sources = _sources(value['sources'])
    if (value['project_directory'] != str(sources[0].parent) or generation.parent != sources[0].parent/'approved-v6'
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
    _check_inputs(raws[:3],raws[3])
    return value,Deployment(tuple(generation/name for name in NAMES),generation/'.env',sources[0].parent,value['project_name'])


def approve_deployment(generation, *, reviewed_sha256):
    _require_root()
    if not re.fullmatch('[0-9a-f]{64}',reviewed_sha256): raise ValueError('exact reviewed SHA required')
    generation = _absolute(generation)
    _load_generation(generation,reviewed_sha256)
    current = generation.parent/'current.json'
    if current.exists(): _trusted_path(current)
    _write(current,_canonical(dict(version=1,generation=generation.name,manifest_sha256=reviewed_sha256)),replace=True)
    return current


def resolve_deployment(compose_files):
    files = _sources(compose_files)
    current = files[0].parent/'approved-v6/current.json'; _trusted_path(current)
    value = _json(_read(current))
    if (type(value) is not dict or set(value) != {'version','generation','manifest_sha256'} or value['version'] != 1
            or type(value['generation']) is not str or not re.fullmatch(r'generation-[0-9a-f]{24}',value['generation'])
            or type(value['manifest_sha256']) is not str or not re.fullmatch('[0-9a-f]{64}',value['manifest_sha256'])):
        raise ValueError('invalid approved deployment selection')
    metadata,deployment = _load_generation(current.parent/value['generation'],value['manifest_sha256'])
    if metadata['sources'] != [str(p) for p in files]:
        raise ValueError('approved three-source deployment differs from configuration')
    return deployment


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    stage=sub.add_parser('stage'); stage.add_argument('--compose',type=Path,action='append',required=True)
    stage.add_argument('--env-file',type=Path); stage.add_argument('--project-name',default='anime-db',choices=('anime-db','animedb-v6-lab'))
    approve=sub.add_parser('approve'); approve.add_argument('--generation',type=Path,required=True); approve.add_argument('--reviewed-sha256',required=True)
    args=parser.parse_args(argv)
    try:
        if args.command == 'stage':
            path=stage_deployment(args.compose,env_file=args.env_file,project_name=args.project_name)
            print(json.dumps(dict(generation=str(path),reviewed_sha256=generation_sha256(path))))
        else:
            print(json.dumps(dict(current=str(approve_deployment(args.generation,reviewed_sha256=args.reviewed_sha256)))))
        return 0
    except (ValueError,OSError) as exc:
        print('STOP: '+str(exc),file=__import__('sys').stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
