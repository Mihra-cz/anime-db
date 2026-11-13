"""M3: real Compose interpolation only; never contact a Docker daemon or deploy."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT=Path(__file__).parents[1]/'deploy/nexus'


def command(tmp_path, *, control):
    if not shutil.which('docker'):pytest.skip('Docker Compose client required')
    env=tmp_path/'.env'
    env.write_text('ANIMEDB_UID=1000\nANIMEDB_GID=1000\n'+('ANIMEDB_CONTROL_GID=2001\n' if control else ''))
    override=tmp_path/'compose.v6.yaml'
    override.write_text('services:\n  app:\n    image: anime-db:v6-db40987\n    build: !reset null\n')
    argv=['docker','compose','--project-directory',str(tmp_path),'--env-file',str(env),'-p','anime-db',
          '-f',str(ROOT/'compose.yaml'),'-f',str(ROOT/'service-edge/compose.edge.yaml'),'-f',str(override)]
    process_env={k:v for k,v in os.environ.items() if not k.startswith(('ANIMEDB_','COMPOSE_'))}
    return argv,process_env


def test_three_production_layers_render_with_explicit_socket_gid(tmp_path):
    argv,env=command(tmp_path,control=True)
    result=subprocess.run(argv+['config','--format','json'],env=env,capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr
    model=json.loads(result.stdout)
    assert model['name']=='anime-db'
    assert model['services']['app']['image']=='anime-db:v6-db40987'
    assert model['services']['app']['user']=='1000:1000'
    assert model['services']['edge']['user']=='101:101'
    assert model['services']['edge']['group_add']==['2001']


@pytest.mark.parametrize('operation',[
    ['config','-q'],['config','--images','app'],['ps','-a'],['logs','--tail','1','app'],['stop','app']])
def test_missing_control_gid_stops_every_operational_command_at_interpolation(tmp_path,operation):
    argv,env=command(tmp_path,control=False)
    result=subprocess.run(argv+operation,env=env,capture_output=True,text=True,timeout=15)
    assert result.returncode!=0
    assert 'ANIMEDB_CONTROL_GID' in result.stderr
    assert 'dedicated host socket group GID' in result.stderr
