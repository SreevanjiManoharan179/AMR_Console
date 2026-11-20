#!/usr/bin/env python3
"""Contained Ollama launch commands, to be used only after install approval.

Bubblewrap is already present. A private tmpfs home holds the runner's transient
identity; its startup stdout is suppressed so no public key is printed or logged.
No owner credentials are read. All persistent runner/cache/model files stay in
.runtime, while the rest of the host filesystem is read-only. HOME is not changed.
"""
import argparse
import os
from pathlib import Path
import shutil
import subprocess

ROOT=Path(__file__).resolve().parent.parent
MODELS=('qwen3.5:2b-q4_K_M','qwen3.5:4b-q4_K_M')


def isolation_command():
    # The actual user home is hidden by a private in-memory mount. The project
    # path is then restored read-only, with only .runtime mounted writable.
    user_home=Path.home()
    if not ROOT.is_relative_to(user_home) or ROOT==user_home:
        raise ValueError('Module must be below the user home for this recipe')
    return ['bwrap','--die-with-parent','--unshare-pid','--ro-bind','/','/',
            '--dev-bind','/dev','/dev','--proc','/proc','--tmpfs',str(user_home),
            '--dir',str(ROOT.parent),'--ro-bind',str(ROOT),str(ROOT),'--tmpfs','/tmp',
            '--chdir',str(ROOT)]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['check-isolation','serve','version','list','pull','stop','rm'])
    parser.add_argument('model',nargs='?',choices=MODELS)
    parser.add_argument('--parallel',type=int,choices=(1,4),default=1)
    parser.add_argument('--max-queue',type=int,choices=(1,8),default=1)
    parser.add_argument('--gpu-overhead-gib',type=int,choices=(0,4),default=4)
    args=parser.parse_args()
    if not shutil.which('bwrap'):
        parser.error('Bubblewrap is unavailable; do not install it without separate approval')
    clean_env={key:os.environ[key] for key in ('HOME','USER','LOGNAME','LANG','PATH') if key in os.environ}
    if args.command=='check-isolation':
        return subprocess.run([*isolation_command(),'--','/usr/bin/true'],env=clean_env,check=False).returncode
    if (args.command in ('pull','stop','rm')) != (args.model is not None):
        parser.error('Specify a model only for pull, stop or rm')
    binary=ROOT/'.runtime/ollama/bin/ollama'
    if not binary.is_file():
        parser.error('Approved Ollama binary is not installed; see docs/INSTALL_REQUEST.md')
    runtime=ROOT/'.runtime'
    for directory in ('models','tmp','cache'):
        (runtime/directory).mkdir(parents=True,exist_ok=True)
    clean_env.update({
        'OLLAMA_HOST':'127.0.0.1:11434','OLLAMA_MODELS':str(runtime/'models'),
        'OLLAMA_NO_CLOUD':'1','OLLAMA_NOHISTORY':'1','OLLAMA_NOPRUNE':'1',
        'OLLAMA_NUM_PARALLEL':str(args.parallel),'OLLAMA_MAX_LOADED_MODELS':'1','OLLAMA_MAX_QUEUE':str(args.max_queue),
        'OLLAMA_CONTEXT_LENGTH':'4096','OLLAMA_GPU_OVERHEAD':str(args.gpu_overhead_gib*1024**3),
        'OLLAMA_KEEP_ALIVE':'5m','TMPDIR':str(runtime/'tmp'),
        'XDG_CACHE_HOME':str(runtime/'cache'),'CUDA_CACHE_PATH':str(runtime/'cache/cuda'),
    })
    command='--version' if args.command=='version' else args.command
    argv=[*isolation_command(),'--bind',str(runtime),str(runtime),'--',str(binary),command]
    if args.model:
        argv.append(args.model)
    return subprocess.run(argv,env=clean_env,
                          stdout=subprocess.DEVNULL if args.command=='serve' else None,
                          check=False).returncode


if __name__=='__main__':
    raise SystemExit(main())
