"""Measurement helpers for read-only model diagnostics. No ROS or dispatch code."""
import json
import math
from pathlib import Path
import subprocess
import threading
import time


def wilson(successes,total):
    if not total:
        return {'successes':successes,'total':total,'rate':None,'wilson95':None}
    z=1.959963984540054
    p=successes/total
    scale=1+z*z/total
    centre=(p+z*z/(2*total))/scale
    half=z*math.sqrt(p*(1-p)/total+z*z/(4*total*total))/scale
    return {'successes':successes,'total':total,'rate':p,
            'wilson95':[max(0.,centre-half),min(1.,centre+half)]}


def percentile(values,quantile):
    if not values:
        return None
    ordered=sorted(values)
    index=max(0,math.ceil(quantile*len(ordered))-1)
    return ordered[index]


class ResourceMonitor:
    def __init__(self,path,interval=.1):
        self.path=Path(path)
        self.interval=interval
        self.stopped=threading.Event()
        self.thread=None
        self.peak_total_gpu_mib=None
        self.peak_compute_gpu_mib=None
        self.minimum_available_ram_kib=None
        self.rows=0

    def start(self):
        self.thread=threading.Thread(target=self._run,daemon=True)
        self.thread.start()
        return self

    def _run(self):
        with self.path.open('w') as output:
            while not self.stopped.is_set():
                started=time.monotonic()
                row={'time_ns':time.time_ns(),'monotonic_s':started}
                try:
                    gpu=subprocess.run(['nvidia-smi','--query-gpu=memory.used,memory.free,utilization.gpu',
                                        '--format=csv,noheader,nounits'],capture_output=True,text=True,timeout=3)
                    row['gpu_exit_code']=gpu.returncode
                    if gpu.returncode==0:
                        values=[int(value.strip()) for value in gpu.stdout.strip().splitlines()[0].split(',')]
                        row.update(gpu_used_mib=values[0],gpu_free_mib=values[1],gpu_utilization_percent=values[2])
                        self.peak_total_gpu_mib=max(self.peak_total_gpu_mib or 0,values[0])
                    compute=subprocess.run(['nvidia-smi','--query-compute-apps=pid,process_name,used_gpu_memory',
                                            '--format=csv,noheader,nounits'],capture_output=True,text=True,timeout=3)
                    row['compute_exit_code']=compute.returncode
                    processes=[]
                    if compute.returncode==0:
                        for line in compute.stdout.strip().splitlines():
                            fields=[field.strip() for field in line.split(',')]
                            if len(fields)==3 and fields[2].isdigit():
                                processes.append({'pid':int(fields[0]),'process_name':fields[1],'used_mib':int(fields[2])})
                        amount=sum(process['used_mib'] for process in processes)
                        row['compute_processes']=processes
                        self.peak_compute_gpu_mib=max(self.peak_compute_gpu_mib or 0,amount)
                    for line in Path('/proc/meminfo').read_text().splitlines():
                        if line.startswith('MemAvailable:'):
                            available=int(line.split()[1])
                            row['available_ram_kib']=available
                            self.minimum_available_ram_kib=min(self.minimum_available_ram_kib or available,available)
                except (OSError,ValueError,IndexError,subprocess.TimeoutExpired) as error:
                    row['error']=type(error).__name__
                output.write(json.dumps(row)+'\n')
                output.flush()
                self.rows+=1
                self.stopped.wait(max(0.,self.interval-(time.monotonic()-started)))

    def stop(self):
        self.stopped.set()
        if self.thread:
            self.thread.join(timeout=8.)
        return {'resource_samples':self.rows,'target_interval_s':self.interval,
                'peak_total_gpu_mib':self.peak_total_gpu_mib,
                'peak_all_compute_processes_gpu_mib':self.peak_compute_gpu_mib,
                'minimum_available_ram_kib':self.minimum_available_ram_kib,
                'limitation':'Sampled peaks can miss brief spikes; compute allocation includes all visible compute processes.'}
