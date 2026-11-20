#!/usr/bin/env python3
"""Reproduce intentional cancellation outside product/quality measurements."""
import argparse
import http.client
import json
from pathlib import Path
import socket
import threading
import time
from runner_control import Runner,ROOT,request
from bench_utils import ResourceMonitor


def abort_generation(model,stream):
    connection=http.client.HTTPConnection('127.0.0.1',11434,timeout=60)
    active=[None];aborted=threading.Event();started=time.monotonic()
    def abort():
        aborted.set()
        sock=active[0] or connection.sock
        if sock:
            try: sock.shutdown(socket.SHUT_RDWR)
            except OSError: pass
        # Shutdown interrupts the reader; its owning thread closes the HTTP object.
    timer=threading.Timer(1.,abort)
    received=0;first_chunk_ms=None;error=None;done=False
    try:
        payload={'model':model,'stream':stream,'think':False,'keep_alive':'30m',
                 'prompt':'Write a long numbered list from 1 to 1000, with a full sentence about each number. Continue until 1000.',
                 'options':{'num_ctx':4096,'num_predict':1536,'temperature':.7,'seed':500}}
        connection.request('POST','/api/generate',json.dumps(payload),{'Content-Type':'application/json'})
        active[0]=connection.sock;timer.start()
        response=connection.getresponse()
        while True:
            chunk=response.read1(4096)
            if not chunk: break
            if first_chunk_ms is None: first_chunk_ms=(time.monotonic()-started)*1000
            received+=len(chunk)
            if b'"done":true' in chunk or b'"done": true' in chunk: done=True
    except (OSError,ValueError,http.client.HTTPException) as exc: error=type(exc).__name__
    finally: timer.cancel();connection.close()
    return {'stream':stream,'abort_requested_after_s':1,'elapsed_ms':(time.monotonic()-started)*1000,
            'bytes_received':received,'first_chunk_ms':first_chunk_ms,'aborted':aborted.is_set(),
            'completed_before_abort':done,'error':error}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--contestant',choices=['A','B'],default='A')
    parser.add_argument('--output',required=True);args=parser.parse_args()
    directory=(ROOT/args.output).resolve()
    if not directory.is_relative_to(ROOT/'records/task3') or directory.exists(): raise ValueError('Use a new task3 folder')
    directory.mkdir(parents=True)
    model=json.loads((ROOT/f'data/office_model_{args.contestant}.json').read_text())['model']
    results=[]
    for stream,queue in [(False,1),(True,1),(False,8),(True,8)]:
        sub=directory/f'stream_{stream}_queue_{queue}'
        with Runner(sub,max_queue=queue) as runner:
            monitor=ResourceMonitor(sub/'resources.jsonl').start()
            try:
                runner.load(model)
                if not runner.ensure_live(): raise RuntimeError('Could not establish live runner')
                before=runner.allocation()
                abort=abort_generation(model,stream)
                immediate=runner.probe()
                # A bounded, non-aborting 60s probe tests eventual self-recovery.
                eventual=runner.probe(timeout=60) if not immediate['ok'] else None
                results.append({'stream':stream,'max_queue':queue,'before':before,'abort':abort,
                                'immediate':immediate,'eventual':eventual,'resources':monitor.stop()})
            finally:
                if monitor.thread and monitor.thread.is_alive(): monitor.stop()
        (directory/'results.json').write_text(json.dumps({'contestant':args.contestant,'experiments':results,
            'note':'Intentional aborts occur only in this reproduction, not in quality measurements.'},indent=2)+'\n')
        print(json.dumps({'stream':stream,'queue':queue,'immediate_ok':immediate['ok'],
                          'eventual_ok':eventual['ok'] if eventual else None}),flush=True)


if __name__=='__main__': main()
