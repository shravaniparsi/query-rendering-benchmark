"""Persistent cpuset-contained Node/Chromium probes with same-host clock checks."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import psutil
from .cpu_budget import tree,verify_browser_sample
from .runtime import ROOT


def check_clock(output):
    script="process.stdout.write('ready\\n');process.stdin.once('data',()=>{process.stdout.write(String(process.hrtime.bigint())+'\\n');});"
    process=subprocess.Popen([shutil.which('node'),'-e',script],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
    try:
        if process.stdout.readline().strip()!='ready':raise RuntimeError('Node clock handshake missing')
        before=time.perf_counter_ns();process.stdin.write('clock\n');process.stdin.flush()
        child=int(process.stdout.readline().strip());after=time.perf_counter_ns();process.stdin.close();process.wait(timeout=5)
        if process.returncode or not before<=child<=after:raise RuntimeError('Python/Node monotonic clock origins differ')
        info=time.get_clock_info('perf_counter')
        if not info.monotonic or info.adjustable or (after-before)>50_000_000:raise RuntimeError('clock bracket exceeds contract')
        value={'role':'same-host Python perf_counter and Node hrtime clock contract','status':'passed',
            'before_ns':str(before),'node_ns':str(child),'after_ns':str(after),'bracket_width_ms':(after-before)/1e6,
            'implementation':info.implementation,'monotonic':info.monotonic,'adjustable':info.adjustable,'resolution_s':info.resolution}
        output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps(value,indent=2)+'\n');return value
    finally:
        if process.poll() is None:process.kill();process.wait(timeout=5)
        if not process.stdin.closed:process.stdin.close()
        process.stdout.close()


class LoadedBrowser:
    def __init__(self,base,directory,case,cpu,spec,*,script=None):
        self.directory=directory;self.cpu=cpu;self.process=None;self.pid=None;self.created=None;self.released=False
        self.script=Path(__file__).with_suffix('.cjs') if script is None else Path(script)
        self.paths={name:str(directory/filename) for name,filename in (
            ('ready_path','loaded-ready.json'),('completed_path','loaded-completed.json'),('report_path','loaded-browser.json'),('screenshot_path','loaded-browser.png'))}
        self.config={'app_url':base,'strategy':case['strategy'],'prefix':case['name'],'duration_s':spec['duration_s'],
            'offsets_s':spec['loaded_browser_offsets_s'],'deadline_ms':spec['loaded_browser_deadline_ms'],
            'max_dispatch_lag_s':spec['loaded_browser_max_dispatch_lag_s'],**self.paths}
    def launch(self):
        config=self.directory/'loaded-browser-config.json';config.write_text(json.dumps(self.config,indent=2)+'\n')
        command=['taskset','-c',str(self.cpu),'sudo','-n','--preserve-env=HOME,PATH,LD_LIBRARY_PATH,PLAYWRIGHT_BROWSERS_PATH',
            sys.executable,str(ROOT/'experiments/query_rendering_load/cpu_exec.py'),'--cpu',str(self.cpu),
            '--uid',str(os.getuid()),'--gid',str(os.getgid()),'--',shutil.which('node'),str(self.script),str(config)]
        self.log=(self.directory/'loaded-browser.log').open('w');before=time.perf_counter_ns()
        self.process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=self.log,stderr=subprocess.STDOUT,text=True)
        deadline=time.monotonic()+35
        while not Path(self.paths['ready_path']).exists():
            if self.process.poll() is not None or time.monotonic()>deadline:raise RuntimeError('loaded browser startup failed')
            time.sleep(.05)
        after=time.perf_counter_ns();ready=json.loads(Path(self.paths['ready_path']).read_text());self.pid=ready['pid']
        root=psutil.Process(self.process.pid);pids={p.pid for p in [root]+root.children(recursive=True)}
        if self.pid not in pids or not before<=int(ready['monotonic_ns'])<=after:raise RuntimeError('loaded browser PID/clock identity differs')
        node=psutil.Process(self.pid);self.created=node.create_time()
        if node.uids().effective!=os.getuid() or Path(node.cmdline()[0]).name not in ('node','nodejs'):raise RuntimeError('non-root Node identity required')
        (self.directory/'loaded-launch.json').write_text(json.dumps({'before_ns':str(before),'after_ns':str(after),'pid':self.pid,'created_epoch_s':self.created})+'\n')
        self.sample();return self
    def sample(self):
        if psutil.Process(self.pid).create_time()!=self.created:raise RuntimeError('loaded Node PID replaced')
        value=tree(self.pid,[self.cpu]);verify_browser_sample(value,self.cpu)
        if not any('chrome' in p['identity']['name'] for p in value['processes'].values()):raise RuntimeError('loaded Chromium tree missing')
        return value
    def start(self,start):
        signal={'measurement_start_monotonic_ns':str(round(start*1e9)),'duration_s':self.config['duration_s']}
        self.start_signal=signal;(self.directory/'loaded-start.json').write_text(json.dumps(signal)+'\n');self.process.stdin.write(json.dumps(signal)+'\n');self.process.stdin.flush()
    def completed(self):return Path(self.paths['completed_path']).exists()
    def wait_completed(self):
        deadline=time.monotonic()+10
        while not self.completed():
            if self.process.poll() is not None or time.monotonic()>deadline:raise RuntimeError('loaded browser probes did not drain')
            time.sleep(.05)
    def release(self):
        self.process.stdin.write(json.dumps({'release':True})+'\n');self.process.stdin.flush();self.released=True
        self.process.wait(timeout=15)
        if self.process.returncode:raise RuntimeError('loaded browser process failed')
        value=json.loads(Path(self.paths['report_path']).read_text())
        if value['status']!='completed' or value['start_signal']!=self.start_signal:raise RuntimeError('loaded browser protocol incomplete')
        return value
    def close(self):
        if self.process and self.process.poll() is None:
            try:self.process.stdin.write(json.dumps({'abort':True})+'\n');self.process.stdin.flush();self.process.wait(timeout=5)
            except (OSError,subprocess.TimeoutExpired):self.process.kill();self.process.wait(timeout=5)
        if hasattr(self,'log'):self.log.close()
