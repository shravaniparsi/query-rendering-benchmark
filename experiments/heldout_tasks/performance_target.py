"""Release-gated fresh fixtures, explicit windows and pre-load policy commitment."""
from contextlib import ExitStack
from datetime import datetime,timezone
import hashlib
import json
import os
import re
import subprocess
import time

from .inventory import require
from .main_design import cells
from .performance_runtime import performance_runtime
from .performance_evidence import digest
from .scale_reference import InventoryScaleReference,AnalyticsScaleReference
from experiments.issue_dashboard.cpu_budget import bind_current,bind_overlay,bind_external_tree
from experiments.issue_dashboard.offhost_resources import ResourceTarget,BudgetWatch
from experiments.issue_dashboard.offhost_engineering import RouteWatch,save,verify_routes
from experiments.issue_dashboard.wal_observer import delta
from scripts.offhost_connectivity import network_state


class PerformanceTarget(ResourceTarget):
    def __init__(self,dsn,output,own,peer_name,task,day,smoke,permission,public_origin):
        self.permission=permission;self.task=task;self.day=day;self.smoke=smoke;self.public_origin=public_origin
        super().__init__(dsn,output,own,peer_name,0,smoke,spec=permission.artifacts['profile_protocol'],cases=cells(permission.artifacts,task,day,smoke))
        self.active_window=None;self.windows=[];self.commitment=None
        self.app_mask=self.host_cpus[:1];self.db_mask=self.host_cpus[1:2]
        bind_current(self.app_mask);bind_overlay(self.overlay,self.app_mask)
        subprocess.run(['docker','update','--cpuset-cpus',','.join(map(str,self.db_mask)),os.environ['QRL_POSTGRES_CONTAINER']],check=True,capture_output=True,text=True)
        bind_external_tree(int(os.environ['QRL_POSTGRES_HOST_PID']),self.db_mask)

    def control(self,name,payload):
        with self.lock:
            self.last=time.monotonic();require(datetime.now(timezone.utc).date().isoformat()==self.day,'acquisition crossed its fixed UTC date')
            if name=='commit-policy':
                require(self.runtime is None and self.index==18 and self.commitment is None,'policy must be committed once after all serial cells and before any loaded cell')
                require(set(payload)=={'choices_sha256','profiles_sha256','release_sha256','collection_date_utc'} and
                    all(isinstance(payload[k],str) and re.fullmatch('[0-9a-f]{64}',payload[k]) for k in ('choices_sha256','profiles_sha256')) and
                    payload['release_sha256']==self.permission.expected_sha256 and payload['collection_date_utc']==self.day,'invalid policy commitment')
                self.commitment={**payload,'target_monotonic_ns':str(time.perf_counter_ns()),'completed_cases':self.index,'loaded_cases_started':0}
                save(self.output/'policy-commitment.json',self.commitment);return self.commitment
            if name=='begin':
                require(self.remote is not None and self.runtime is None and self.index<len(self.cases),'invalid begin state');case=self.cases[self.index]
                require(payload=={'case':case['name'],'observer':self.remote},'unexpected case/observer')
                if case['phase']=='loaded':require(self.commitment is not None,'loaded outcomes are closed until policy commitment')
                self.case=case;self.windows=[];self.active_window=None;self.wal_before=None
                directory=self.output/case['name'];directory.mkdir();self.stack=ExitStack()
                self.runtime=self.stack.enter_context(performance_runtime(self.dsn,directory,self.task,case,self.permission,target_cpu=self.app_mask,public_origin=self.public_origin))
                save(directory/'database.json',self.runtime['origin'].command('diagnostics'))
                save(directory/'budget-assignment.json',{'app_mask':self.app_mask,'db_mask':self.db_mask,'selected_host_cpus':self.host_cpus,'full_host_cpus':self.own['allowed_cpus'],'gateway_and_overlay_in_app_budget':True})
                network_state(self.own['overlay_name'],self.peer_name,timeout=30,require_direct=True)
                self.routes=RouteWatch(self.own['overlay_name'],self.peer_name).start();return {'case':case,'ready':True}
            require(self.runtime is not None and payload.get('case')==self.case['name'],'no matching active case');directory=self.output/self.case['name']
            if name=='window-begin':
                label=payload.get('label');enabled=payload.get('instrumented')
                require(set(payload)=={'case','label','instrumented'} and type(enabled) is bool and isinstance(label,str) and re.fullmatch('[a-z0-9-]{1,50}',label) and
                    self.active_window is None and label not in [v['label'] for v in self.windows],'window already active/invalid')
                self.wal_before=self.runtime['origin'].command('wal-snapshot');save(directory/(label+'-wal-before.json'),self.wal_before)
                self.active_window={'label':label,'instrumented':enabled,'start_monotonic_ns':str(time.perf_counter_ns())}
                self.budget=BudgetWatch(self.capture).start() if enabled else None;return {'begun':True}
            if name=='window-end':
                require(set(payload)=={'case','label'} and self.active_window is not None and payload['label']==self.active_window['label'],'window not active');value=self.active_window
                if self.budget:save(directory/(value['label']+'-budget.json'),self.budget.finish());self.budget=None
                value['collection_finish_monotonic_ns']=str(time.perf_counter_ns())
                if self.case['phase']!='qualification':self.runtime['quiesce']()
                else:require(self.runtime['origin'].command('wait-idle'),'qualification origin did not drain')
                after=self.runtime['origin'].command('wal-snapshot');value['counter_finish_monotonic_ns']=str(time.perf_counter_ns())
                value['counter_scope']='Pool counters include completed native transactions after receipt/probe collection and before this quiescent boundary.'
                save(directory/(value['label']+'-wal-after.json'),after);save(directory/'wal-after.json',after);save(directory/(value['label']+'-wal-delta.json'),delta(self.wal_before,after,False))
                self.windows.append(value);self.active_window=None;save(directory/'windows.json',self.windows);return {'ended':True}
            if name=='end':
                require(payload=={'case':self.case['name']} and self.active_window is None and self.windows,'measurement not closed')
                samples=self.routes.finish();self.routes=None;save(directory/'routes.json',samples);verify_routes(samples)
                self.runtime['quiesce']();database=self.runtime['origin'].command('diagnostics');save(directory/'database-after.json',database)
                state=self.runtime['fixture'].validation_state();save(directory/'validation-state.json',state)
                final=(InventoryScaleReference if self.task=='inventory' else AnalyticsScaleReference)(state).verify_final()
                path=directory/'backend.jsonl';events=[json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
                packet={'case':self.case,'authorization':self.runtime['authorization'],'state':state,'backend_events':events,'windows':self.windows,'database':database,
                    'preparation':json.loads((directory/'preparation.json').read_bytes()),'source_commit':self.own['source_commit'],'release_sha256':self.permission.expected_sha256}
                save(directory/'profile-packet.json',packet)
                self.cells.append({**self.case,'windows':self.windows,'final_oracle':final,'packet_sha256':digest(packet)});self.index+=1
                self.stack.close();self.stack=None;self.runtime=None;return {'closed':True,'packet':packet}
            raise ValueError('unknown released target control')

    def close(self):
        """Retain best-effort active state before schema cleanup on any failure."""
        try:
            if self.runtime is not None:
                directory=self.output/self.case['name']
                try:
                    if self.budget:save(directory/'interrupted-budget.json',self.budget.finish());self.budget=None
                    self.runtime['quiesce']();save(directory/'interrupted-state.json',self.runtime['fixture'].validation_state())
                    save(directory/'interrupted-database.json',self.runtime['origin'].command('diagnostics'))
                except Exception as error:save(directory/'interrupted-state-failure.json',{'error_type':type(error).__name__,'error':str(error)[:400]})
        finally:super().close()
