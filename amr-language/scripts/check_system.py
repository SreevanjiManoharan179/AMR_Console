#!/usr/bin/env python3
"""Read-only host readiness check. Stores evidence only inside this module.

No installs, no ROS imports, no motion, no environment dump or command arguments.
Optional model probe contacts only literal 127.0.0.1 and never pulls a model.
"""
import argparse
from datetime import datetime
import http.client
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parent.parent


def command(argv):
    try:
        result=subprocess.run(argv,capture_output=True,text=True,timeout=10,check=False)
        return {'argv':argv,'exit_code':result.returncode,'stdout':result.stdout,'stderr':result.stderr}
    except (OSError,subprocess.TimeoutExpired) as exc:
        return {'argv':argv,'exit_code':None,'error':type(exc).__name__}


def probe():
    result={}
    for path in ('/api/version','/api/tags'):
        connection=http.client.HTTPConnection('127.0.0.1',11434,timeout=1.)
        try:
            connection.request('GET',path)
            response=connection.getresponse()
            raw=response.read(262145)
            result[path]={'status':response.status,'response':json.loads(raw) if len(raw)<=262144 else 'too large'}
        except (OSError,ValueError,http.client.HTTPException) as exc:
            result[path]={'error':type(exc).__name__}
        finally:
            connection.close()
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',default='records/checks/system.json')
    parser.add_argument('--probe-model',action='store_true')
    args=parser.parse_args()
    output=(ROOT/args.output).resolve()
    if not output.is_relative_to(ROOT):
        parser.error('Output must be inside amr-language')
    try:
        import yaml,pytest
        dependencies={'PyYAML':yaml.__version__,'pytest':pytest.__version__}
    except ImportError as exc:
        dependencies={'missing':exc.name}
    memory={}
    for line in Path('/proc/meminfo').read_text().splitlines():
        key,value=line.split(':',1)
        if key in ('MemTotal','MemAvailable','SwapTotal','SwapFree'):
            memory[key]=value.strip()
    processes=command(['ps','-eo','comm='])
    names=processes.get('stdout','').splitlines()
    interesting=sorted({name.strip() for name in names if any(token in name.casefold() for token in
                        ('gz','rviz','nav2','controller_ser','planner_server','ollama','llama','python'))})
    result={'timestamp':datetime.now(ZoneInfo('Asia/Kolkata')).isoformat(),
            'grade':'checked','python':platform.python_version(),'os':platform.system(),
            'memory':memory,'disk_free_bytes':shutil.disk_usage(ROOT).free,'dependencies':dependencies,
            'executables':{name:shutil.which(name) for name in ('ollama','llama-server','ros2','gz','nvidia-smi','tar','zstd','curl')},
            'gpu':command(['nvidia-smi','--query-gpu=name,driver_version,memory.total,memory.used,memory.free','--format=csv,noheader']),
            'gpu_compute_processes':command(['nvidia-smi','--query-compute-apps=process_name,used_gpu_memory','--format=csv,noheader']),
            'relevant_process_names':interesting,'process_listing_exit_code':processes['exit_code'],
            'navigation_running':'not established: process names alone do not prove an active navigation session',
            'coexistence_benchmark':'not run'}
    if args.probe_model:
        result['local_model_server']=probe()
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
