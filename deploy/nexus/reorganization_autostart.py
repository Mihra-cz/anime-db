#!/usr/bin/env python3
"""Boot/stop the existing production project from its single approved generation.

This helper never starts the coordinator, retries E7, builds or pulls an image.
Installing it into the actual host service requires review of that service.
"""
import argparse
import re
import hashlib
from pathlib import Path
try:
    from reorganization_coordinator import secure_root_path, read_json, run_command
    from reorganization_deployment import resolve_deployment, _read, _json, ensure_bindings, active_commit, verify_active_release
except ImportError:
    from deploy.nexus.reorganization_coordinator import secure_root_path, read_json, run_command
    from deploy.nexus.reorganization_deployment import resolve_deployment, _read, _json, ensure_bindings, active_commit, verify_active_release


def compose(deployment):
    argv = ['/usr/bin/docker','compose','--project-directory',str(deployment.project_directory),
        '--env-file',str(deployment.env_file),'-p',deployment.project_name]
    for path in deployment.files:
        argv += ['-f',str(path)]
    return argv



def load_boot(path, *, verify_live=True, project_name='anime-db'):
    secure_root_path(path)
    config = read_json(path)
    files = tuple(Path(p) for p in config['compose_files'])
    root = Path(config['approved_root'])
    deployment = resolve_deployment(files,approved_root=root)
    if project_name not in ('anime-db', 'animedb-v6-lab') or deployment.project_name != project_name:
        raise ValueError('approved deployment project differs from explicit host profile')
    if not verify_live:
        return deployment,config
    verify_active_release(deployment,files,config['image'])
    return deployment,config


def autostart(deployment, config, action, *, run=run_command, project_name='anime-db'):
    if project_name not in ('anime-db', 'animedb-v6-lab') or deployment.project_name != project_name:
        raise ValueError('approved deployment project differs from explicit host profile')
    argv = compose(deployment)
    if action == 'stop':
        # An app shutdown for maintenance must leave the service page available.
        return run(argv+['stop','-t','30','app'])
    if action != 'start': raise ValueError('start or stop required')
    ensure_bindings(getattr(deployment,'bindings',()))
    model = _json(run(argv+['config','--format','json']))
    services = model.get('services',{})
    app,edge = services.get('app',{}),services.get('edge',{})
    entrypoint = (['python','/usr/local/libexec/anime-db/nexus_first_run.py']
        if project_name == 'anime-db' else ['python','/lab/lab_entrypoint.py'])
    if (model.get('name') != project_name or app.get('image') != config['image']
            or app.get('user') != f"{config['uid']}:{config['gid']}"
            or app.get('entrypoint') != entrypoint
            or edge.get('user') != '101:101'
            or str(config['socket_gid']) not in [str(g) for g in edge.get('group_add',[])]
            or not re.fullmatch(r'nginx:[^@]+@sha256:[0-9a-f]{64}',edge.get('image',''))):
        raise ValueError('approved boot app/edge identity or startup guard differs')
    fixed = ['up','-d','--no-deps','--no-build','--pull=never']
    run(argv+fixed+['edge'])
    return run(argv+fixed+['app'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,default=Path('/etc/anime-db/reorganization-coordinator.json'))
    parser.add_argument('action',choices=('start','stop'))
    args = parser.parse_args()
    try:
        deployment,config = load_boot(args.config,verify_live=args.action=='start')
        autostart(deployment,config,args.action)
    except (OSError,ValueError,KeyError,TypeError,RuntimeError) as exc:
        parser.exit(2,'STOP: '+str(exc)+'\n')


if __name__ == '__main__':
    main()
