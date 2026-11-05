"""Explicit logical-CPU masks and live process/thread observation."""
import os
from pathlib import Path
import time
import subprocess
import psutil


class NoLiveTasks(RuntimeError):
    """The observed process tree exited before any live task could be read."""


def mask(value):
    if type(value) is int:values=[value]
    elif isinstance(value,(list,tuple,set)):values=list(value)
    else:raise ValueError('integer CPU or explicit CPU collection required')
    if not values or any(type(v) is not int or v<0 for v in values) or len(set(values))!=len(values):
        raise ValueError('distinct nonnegative logical CPU IDs required')
    return sorted(values)


def bind_current(values):
    values=mask(values)
    for entry in Path('/proc/self/task').iterdir():
        try:os.sched_setaffinity(int(entry.name),set(values))
        except ProcessLookupError:pass


def overlay_pid():
    found=[p.pid for p in psutil.process_iter(['name']) if p.info['name']=='tailscaled']
    if len(found)!=1:raise RuntimeError('one overlay daemon required')
    return found[0]


def bind_overlay(pid,values):
    subprocess.run(['sudo','taskset','-a','-p','-c',','.join(map(str,mask(values))),str(pid)],check=True,capture_output=True,text=True)


def bind_external_tree(pid,values):
    root=psutil.Process(pid)
    for process in [root]+root.children(recursive=True):
        try:bind_overlay(process.pid,values)
        except subprocess.CalledProcessError:
            if process.is_running():raise


def tree(pid,expected,recursive=True):
    expected=mask(expected);root=psutil.Process(pid);records=[];disappeared=[];processes={}
    for process in [root]+(root.children(recursive=True) if recursive else []):
        try:
            identity={'pid':process.pid,'created_epoch_s':process.create_time(),'name':process.name()}
            usage={'identity':identity,'cpu_times':process.cpu_times()._asdict(),'rss_bytes':process.memory_info().rss}
            try:usage['io']=process.io_counters()._asdict()
            except (psutil.AccessDenied,AttributeError):usage['io']=None
            processes[f'{process.pid}:{identity["created_epoch_s"]}']=usage
            for entry in Path(f'/proc/{process.pid}/task').iterdir():
                try:
                    allowed=sorted(os.sched_getaffinity(int(entry.name)))
                    cgroups=(entry/'cgroup').read_text().splitlines()
                    unified=[line.split(':',2)[2] for line in cgroups if line.startswith('0::')]
                    records.append({**identity,'tid':int(entry.name),'allowed_cpus':allowed,'expected_cpus':expected,
                                    'matches':allowed==expected,'unified_cgroup':unified[0] if len(unified)==1 else None})
                except (FileNotFoundError,ProcessLookupError):disappeared.append(int(entry.name))
        except (FileNotFoundError,ProcessLookupError,psutil.NoSuchProcess):disappeared.append(process.pid)
    if not records:raise NoLiveTasks('no live tasks to observe')
    return {'monotonic_s':time.perf_counter(),'tasks':records,'processes':processes,'disappeared_tasks':disappeared,
            'status':'passed' if all(r['matches'] for r in records) else 'failed'}


def verify_browser_sample(sample,cpu):
    if sample['status']!='passed' or not sample['tasks']:
        raise RuntimeError('browser task coverage/masks failed')
    for task in sample['tasks']:
        if (not task['matches'] or task['allowed_cpus']!=[cpu] or task['expected_cpus']!=[cpu]
                or task['unified_cgroup']!='/qrl_browser_observer'):
            raise RuntimeError('browser CPU or kernel cpuset containment differs')


def browser_sample(pid,cpu):
    try:value=tree(pid,[cpu])
    except NoLiveTasks:return None
    verify_browser_sample(value,cpu)
    return value
