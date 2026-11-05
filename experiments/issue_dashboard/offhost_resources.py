"""Budgeted two-VM resource/WAL diagnostic with paired instrumentation controls."""
from contextlib import ExitStack
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import threading
import time

from scripts.offhost_connectivity import manifest,node_name,network_state,private_ip,check_pair
from experiments.query_rendering_load.arrivals import execute
from experiments.query_rendering_load.calibrate import summarize
from .offhost_engineering import Target,gateway,RouteWatch,verify_routes,save,request_json,require
from .offhost_resource_design import ROLE,SEED_BASE,design,cases_for
from .cpu_budget import bind_current,bind_overlay,bind_external_tree,overlay_pid,tree,browser_sample
from .runtime import ROOT,issue_runtime
from .arrivals import operation,schedule
from .oracle import IssueOracle
from .wal_observer import delta as wal_delta


class BudgetWatch:
    def __init__(self,capture):self.capture=capture;self.samples=[];self.errors=[];self.stop=threading.Event();self.thread=None
    def sample(self):
        try:self.samples.append({'monotonic_s':time.perf_counter(),'groups':self.capture()})
        except Exception as error:self.errors.append({'error_type':type(error).__name__})
    def start(self):
        self.sample()
        def work():
            while not self.stop.wait(1):self.sample()
        self.thread=threading.Thread(target=work,daemon=True);self.thread.start();return self
    def finish(self):
        self.stop.set();self.thread.join(timeout=12);require(not self.thread.is_alive(),'budget sampler did not stop')
        self.sample();value={'samples':self.samples,'errors':self.errors};verify_budget(value);return value


def verify_budget(value):
    require(not value['errors'] and len(value['samples'])>=2,'budget coverage incomplete')
    for sample in value['samples']:
        for group in sample['groups'].values():
            require(group['tasks'] and group['status']=='passed','empty/failed budget group')
            for task in group['tasks']:require(task['matches'] and task['allowed_cpus']==task['expected_cpus'],'raw mask differs')


def mixed_plan(spec,allocation):
    return schedule(spec['rate'],spec['duration_s'],spec['write_fraction'],spec['rows'],
                    seed=(spec.get('schedule_seed_base',SEED_BASE)+allocation)*1000+spec['rate']*10)


class ResourceTarget(Target):
    def __init__(self,dsn,output,own,peer_name,allocation,smoke,*,spec=None,cases=None):
        super().__init__(dsn,output,own,peer_name)
        self.spec=design(smoke) if spec is None else spec
        self.cases=cases_for(allocation,smoke) if cases is None else cases
        self.budget=None;self.wal_before=None
        self.overlay=overlay_pid();self.host_cpus=sorted(own['allowed_cpus'])[:2]
    def capture(self):
        r=self.runtime
        return {'gateway_controller':tree(os.getpid(),self.app_mask,False),'overlay':tree(self.overlay,self.app_mask,False),
                'origin':tree(r['origin'].pid,self.app_mask),'web':tree(r['process'].pid,self.app_mask),
                'postgres':tree(int(os.environ['QRL_POSTGRES_HOST_PID']),self.db_mask)}
    def control(self,name,payload):
        with self.lock:
            self.last=time.monotonic()
            if name=='begin':
                require(self.remote is not None and self.runtime is None and self.index<len(self.cases),'invalid begin state')
                case=self.cases[self.index];require(payload=={'case':case['name'],'observer':self.remote},'unexpected case/peer')
                self.case=case;self.app_mask=self.host_cpus if case['placement']=='shared' else self.host_cpus[:1]
                self.db_mask=self.host_cpus if case['placement']=='shared' else self.host_cpus[1:2]
                bind_current(self.app_mask);bind_overlay(self.overlay,self.app_mask)
                subprocess.run(['docker','update','--cpuset-cpus',','.join(map(str,self.db_mask)),os.environ['QRL_POSTGRES_CONTAINER']],
                               check=True,capture_output=True,text=True)
                bind_external_tree(int(os.environ['QRL_POSTGRES_HOST_PID']),self.db_mask)
                directory=self.output/case['name'];directory.mkdir();self.stack=ExitStack()
                self.runtime=self.stack.enter_context(issue_runtime(self.dsn,directory,case['query_kind'],self.spec['rows'],self.spec['fanout'],
                    target_cpu=self.app_mask,pool_size=4,prepare_threshold=None,checkpoint=True,
                    write_phase_tracing=case['instrumented'],durable_writes=True,wal_io_timing=case['instrumented']))
                save(directory/'database.json',{'fixture':self.runtime['fixture'].diagnostics(),'service':self.runtime['origin'].command('diagnostics')})
                save(directory/'budget-assignment.json',{'app_mask':self.app_mask,'db_mask':self.db_mask,
                    'gateway_and_overlay_in_app_budget':True,'host_cpus':self.host_cpus})
                network_state(self.own['overlay_name'],self.peer_name,timeout=30,require_direct=True)
                self.routes=RouteWatch(self.own['overlay_name'],self.peer_name).start()
                return {'case':case,'ready':True}
            require(self.runtime is not None and payload=={'case':self.case['name']},'no matching active case')
            directory=self.output/self.case['name']
            if name=='measure-begin':
                require(self.budget is None and self.wal_before is None,'measurement already begun')
                self.wal_before=self.runtime['origin'].command('wal-snapshot');save(directory/'wal-before.json',self.wal_before)
                self.budget=BudgetWatch(self.capture).start();return {'begun':True}
            if name=='measure-end':
                require(self.budget is not None,'measurement not running')
                save(directory/'budget-window.json',self.budget.finish());self.budget=None
                after=self.runtime['origin'].command('wal-snapshot');save(directory/'wal-after.json',after)
                save(directory/'wal-delta.json',wal_delta(self.wal_before,after,self.case['instrumented']))
                return {'ended':True}
            if name=='end':
                require(self.budget is None and (directory/'wal-after.json').exists(),'measurement not closed')
                samples=self.routes.finish();self.routes=None;save(directory/'routes.json',samples);verify_routes(samples)
                save(directory/'database-after.json',self.runtime['origin'].command('diagnostics'))
                self.runtime['quiesce']();state=self.runtime['fixture'].validation_state();save(directory/'validation-state.json',state)
                final=IssueOracle(state).verify_final(state);self.stack.close();self.stack=None;self.runtime=None
                self.cells.append({**self.case,'final_oracle':final});self.index+=1;self.wal_before=None
                return {'closed':True}
            return super().control(name,payload)
    def close(self):
        try:
            if self.budget:
                save(self.output/self.case['name']/'budget-window.json',self.budget.finish());self.budget=None
        finally:super().close()


def browser(base,directory,case,label,cpu):
    import psutil
    config={'app_url':base,'strategy':case['strategy'],'failure':label=='browser-failure','offhost':True,
            'operation_id':case['name']+'-'+label,'report_path':str(directory/(label+'.json')),
            'screenshot_path':str(directory/(label+'.png'))}
    filename=directory/(label+'-config.json');save(filename,config);samples=[]
    with (directory/(label+'.log')).open('w') as log:
        process=subprocess.Popen(['taskset','-c',str(cpu),'sudo','-n',
            '--preserve-env=HOME,PATH,LD_LIBRARY_PATH,PLAYWRIGHT_BROWSERS_PATH',sys.executable,
            str(ROOT/'experiments/query_rendering_load/cpu_exec.py'),'--cpu',str(cpu),
            '--uid',str(os.getuid()),'--gid',str(os.getgid()),'--',
            shutil.which('node'),str(Path(__file__).with_name('browser_check.cjs')),str(filename)],
                                 stdout=log,stderr=subprocess.STDOUT)
        try:
            deadline=time.monotonic()+40
            while process.poll() is None:
                try:
                    info=psutil.Process(process.pid)
                    nodes=[]
                    for candidate in [info]+info.children(recursive=True):
                        command=candidate.cmdline()
                        if command and Path(command[0]).name in ('node','nodejs'):
                            require(candidate.uids().effective==os.getuid(),'browser privilege drop failed');nodes.append(candidate.pid)
                    require(len(nodes)<=1,'multiple browser Node roots')
                    if nodes:
                        sample=browser_sample(nodes[0],cpu)
                        if sample is not None:samples.append(sample)
                except (psutil.NoSuchProcess,ProcessLookupError):pass
                if time.monotonic()>deadline:raise TimeoutError('browser timeout')
                time.sleep(.1)
            require(process.returncode==0,'browser failed')
        finally:
            if process.poll() is None:process.kill();process.wait(timeout=5)
            save(directory/(label+'-placement.json'),samples)
    require(samples and all(s['status']=='passed' for s in samples),'browser mask coverage failed')


def run_observer(output,own,peer_name,peer_ip,allocation,smoke,*,spec=None,cases=None,collected=None,handshake=True,finish=True,probe_callback=None):
    spec=design(smoke) if spec is None else spec;cases=cases_for(allocation,smoke) if cases is None else cases
    cpus=sorted(own['allowed_cpus'])[:2]
    require(len(cpus)==2,'two observer CPUs required');bind_current(cpus[:1]);overlay=overlay_pid();bind_overlay(overlay,cpus[:1])
    base=f'http://{peer_ip}:8765';deadline=time.monotonic()+180
    while True:
        try:remote=request_json(base,'/manifest',timeout=3);break
        except OSError:
            if time.monotonic()>deadline:raise
            time.sleep(1)
    check_pair(own,remote)
    if handshake:request_json(base,'/qrl/hello',own)
    cells=[] if collected is None else collected
    for case in cases:
        cell_spec={**spec,'rate':case.get('rate',spec.get('rate'))}
        loaded=None
        directory=output/case['name'];directory.mkdir()
        require(request_json(base,'/qrl/begin',{'case':case['name'],'observer':own})=={'case':case,'ready':True},'begin differs')
        network_state(own['overlay_name'],peer_name,timeout=30,require_direct=True)
        watch=RouteWatch(own['overlay_name'],peer_name).start()
        try:
            browser(base,directory,case,'browser-before',cpus[1])
            request_json(base,'/qrl/fault-on',{'case':case['name']});browser(base,directory,case,'browser-failure',cpus[1])
            save(directory/'logical-failure.json',operation({'id':0,'kind':'read','project_id':0,'state':'open','limit':20,'offset':0},
                case['strategy'],base,base,case['name']+'-logical-fault',allowed_remote_host=peer_ip))
            request_json(base,'/qrl/fault-off',{'case':case['name']})
            warm=schedule(spec['warmup_rate'],spec['warmup_s'],0,spec['rows'],spec.get('schedule_seed_base',SEED_BASE)+allocation)
            warm_records=execute(warm,lambda r:operation(r,case['strategy'],base,base,case['name']+'-warm',allowed_remote_host=peer_ip),workers=4)
            save(directory/'warmup.json',warm_records);require(all(r['outcome']=='completed' for r in warm_records),'warmup incomplete')
            if probe_callback:probe_callback(base,directory,case,'pre',own,smoke)
            planned=mixed_plan(cell_spec,allocation);save(directory/'schedule.json',planned)
            if spec.get('loaded_browser_offsets_s'):
                from .loaded_browser import LoadedBrowser
                loaded=LoadedBrowser(base,directory,case,cpus[1],spec);loaded.launch()
            request_json(base,'/qrl/measure-begin',{'case':case['name']})
            def capture():
                value={'generator_sampler':tree(os.getpid(),cpus[:1],False),'overlay':tree(overlay,cpus[:1],False)}
                if loaded:value['loaded_browser']=loaded.sample()
                return value
            budget=BudgetWatch(capture).start()
            started=time.perf_counter();records=None
            try:
                records=execute(planned,lambda r:operation(r,case['strategy'],base,base,case['name'],allowed_remote_host=peer_ip),
                                workers=64,drain_timeout=30,start_callback=loaded.start if loaded else None)
                remaining=spec['duration_s']-(time.perf_counter()-started)
                if remaining>0:time.sleep(remaining)
                if loaded:loaded.wait_completed()
            finally:
                if records is not None:save(directory/'operations.json',records)
                save(directory/'budget-window.json',budget.finish())
            summary=summarize(records,spec['duration_s']);require(not summary['censored'],'drain censoring')
            request_json(base,'/qrl/measure-end',{'case':case['name']})
            if loaded:loaded.release()
            if probe_callback:probe_callback(base,directory,case,'post',own,smoke)
            request_json(base,'/qrl/visible-write',{'case':case['name']});browser(base,directory,case,'browser-after',cpus[1])
        finally:
            if loaded:loaded.close()
            routes=watch.finish();save(directory/'routes.json',routes)
        verify_routes(routes);request_json(base,'/qrl/end',{'case':case['name']})
        cells.append({**case,'summary':summary});print(json.dumps({'phase':'cell_complete',**case,'outcomes':summary['outcomes']}),flush=True)
    if finish:request_json(base,'/qrl/finish',{'observer':own})
    return remote,cells


def main():
    import argparse
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--role',choices=('target','observer'),required=True)
    parser.add_argument('--allocation',type=int,choices=(0,1,2),default=0);parser.add_argument('--smoke',action='store_true')
    parser.add_argument('--calibration',action='store_true')
    parser.add_argument('--loaded-browser',action='store_true')
    parser.add_argument('--query-browser',action='store_true')
    parser.add_argument('--matched-path',action='store_true')
    args=parser.parse_args()
    if args.matched_path and not args.query_browser:parser.error('matched path requires query/browser acquisition')
    if sum((args.calibration,args.loaded_browser,args.query_browser))>1:parser.error('select one diagnostic profile')
    from . import offhost_calibration_design,offhost_resource_design,offhost_loaded_design,offhost_acquisition_design
    profile=offhost_acquisition_design if args.query_browser else offhost_loaded_design if args.loaded_browser else offhost_calibration_design if args.calibration else offhost_resource_design
    spec=profile.design(args.smoke);cases=profile.cases_for(args.allocation,args.smoke)
    stage='issue-offhost-acquisition' if args.query_browser else 'issue-offhost-loaded' if args.loaded_browser else 'issue-offhost-calibration' if args.calibration else 'issue-offhost-resources'
    output=Path('ci-artifacts')/stage;output.mkdir(parents=True,exist_ok=False)
    own=manifest(args.role,os.environ['GITHUB_RUN_ID'],os.environ['GITHUB_RUN_ATTEMPT'],os.environ['GITHUB_SHA'])
    peer_name=node_name(own['run_id'],own['run_attempt'],'observer' if args.role=='target' else 'target')
    report={'role':profile.ROLE,'confirmatory_data':False,'local':own,'allocation':args.allocation,'smoke':args.smoke,
            'design':spec,'cases':cases,'cells':[],'status':'started',
            'platform':platform.platform(),'python':platform.python_version(),'runner_image':os.environ.get('ImageVersion'),
            'packages':{n:importlib.metadata.version(n) for n in ('Flask','gunicorn','psycopg','psutil')},
            'source_sha256':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in
                list((ROOT/'experiments/issue_dashboard').glob('*.py'))+list((ROOT/'experiments/issue_dashboard').glob('*.cjs'))+
                list((ROOT/'experiments/query_rendering_load').glob('*.py'))+list((ROOT/'tests').glob('*.py'))+
                list((ROOT/'apps/issue-dashboard').glob('*.py'))+list((ROOT/'apps/issue-dashboard/templates').glob('*.html'))+
                list((ROOT/'apps/issue-dashboard/static').glob('*.js'))+[ROOT/p for p in
                    ('scripts/offhost_connectivity.py','scripts/prepare_browser_cgroups.py','scripts/benchmark_transport_setup.py','.github/workflows/query-rendering-offhost-connectivity.yml',
                     'apps/issue-dashboard/requirements.txt','apps/issue-dashboard/package-lock.json',
                     'scripts/audit_offhost_resources.py','paper/followup/query-rendering-load/protocol/offhost_resources_v1.md')]+
                ([ROOT/p for p in ('scripts/audit_offhost_calibration.py','scripts/analyze_offhost_calibration.py',
                    'paper/followup/query-rendering-load/protocol/offhost_calibration_v1.md')] if args.calibration else [])+
                ([ROOT/p for p in ('scripts/audit_offhost_loaded.py','scripts/analyze_offhost_loaded.py','scripts/check_browser_clock.py',
                    'paper/followup/query-rendering-load/protocol/offhost_loaded_v1.md')] if args.loaded_browser else [])+
                ([ROOT/p for p in ('scripts/audit_offhost_acquisition_transport.py','scripts/replay_offhost_acquisition_transport.py','paper/followup/query-rendering-load/protocol/offhost_proxy_transport_v1_1.md','scripts/audit_offhost_acquisition.py','scripts/check_offhost_acquisition_corruptions.py','scripts/replay_offhost_acquisition_archives.py','scripts/audit_offhost_query_browser.py','scripts/analyze_offhost_query_browser.py','scripts/audit_offhost_serial.py','scripts/check_browser_clock.py',
                    'paper/followup/query-rendering-load/protocol/offhost_query_browser_v1.md','paper/followup/query-rendering-load/protocol/offhost_serial_v1.md',
                    'paper/followup/query-rendering-load/protocol/main_decision_components_v1.json')]+list((ROOT/'experiments/study_design').glob('*.py')) if args.query_browser else [])+
                ([ROOT/p for p in ('scripts/audit_matched_path.py','scripts/replay_matched_path.py','scripts/check_matched_path_corruptions.py',
                    'paper/followup/query-rendering-load/protocol/matched_gateway_path_v1.md')] if args.matched_path else [])}}
    target=server=None
    try:
        if args.query_browser:
            from .proxy_transport import POLICY
            report['proxy_transport_policy']=POLICY
        if args.matched_path:
            from .path_controls import protocol,collect
            report['path_control_protocol']=protocol(args.smoke)
        state=network_state(own['overlay_name'],peer_name,timeout=300);report['network_discovery']=state
        observer_runner=run_observer
        if args.query_browser:
            from .offhost_acquisition import run_observer as observer_runner
        if args.role=='observer':report['remote'],report['cells']=observer_runner(output,own,peer_name,private_ip(state['peer']['TailscaleIPs']),args.allocation,args.smoke,
            spec=spec,cases=cases,collected=report['cells'],**({'probe_callback':collect} if args.matched_path else {}))
        else:
            target=ResourceTarget(os.environ['QRL_TEST_DATABASE_URL'],output,own,peer_name,args.allocation,args.smoke,spec=spec,cases=cases)
            target.smoke=args.smoke
            server=gateway(target,private_ip(state['self']['TailscaleIPs']),private_ip(state['peer']['TailscaleIPs']),classify_proxy_transport=args.query_browser,path_controls=args.matched_path);deadline=time.monotonic()+spec.get('target_deadline_s',1500)
            while not target.finished and time.monotonic()<deadline:
                server.handle_request()
                if time.monotonic()-target.last>spec.get('target_idle_s',180):raise RuntimeError('observer idle timeout')
            require(target.finished and not target.errors,'resource protocol incomplete/errors');report.update(remote=target.remote,cells=target.cells)
        report['status']='passed'
    except Exception as error:report.update(status='failed',error_type=type(error).__name__);raise
    finally:
        try:
            if server:server.server_close()
            if target:
                report.update(gateway_errors=target.errors,cells=target.cells,remote=target.remote)
                if args.query_browser:report['proxy_transport_failures']=target.proxy_transport_failures
                target.close()
        except Exception as error:report.update(status='failed',cleanup_error_type=type(error).__name__);raise
        finally:
            save(output/'report.json',report)
            if args.query_browser:
                from .offhost_acquisition import projections
                for name,value in projections(report).items():save(output/name,value)


if __name__=='__main__':main()
