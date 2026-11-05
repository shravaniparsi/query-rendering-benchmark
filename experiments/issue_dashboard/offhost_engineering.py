"""Two-VM issue-task correctness validation; all timing is engineering evidence."""
from collections import Counter
from contextlib import ExitStack
import hashlib
import importlib.metadata
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import platform
import shutil
import subprocess
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request,build_opener,ProxyHandler

from scripts.offhost_connectivity import manifest,node_name,network_state,private_ip,select_nodes,check_pair
from experiments.query_rendering_load.arrivals import execute
from experiments.query_rendering_load.resource_control import affinity_tree
from .arrivals import operation,schedule
from .oracle import IssueOracle
from .runtime import ROOT,issue_runtime
from .write_phase_metrics import validate_trace
from .fixture import COUNT_SQL,QUERIES

ROLE='two-VM issue-task correctness engineering'
CASES=[{'name':f'cell-{i}','query_kind':q,'strategy':s}
       for i,(q,s) in enumerate((q,s) for q in ('correlated','batched','maintained') for s in ('ssr','csr'))]
TRANSPORT=build_opener(ProxyHandler({}))


def save(path,value):path.write_text(json.dumps(value,indent=2)+'\n')


def request_json(base,path,payload=None,timeout=40):
    headers={'Content-Type':'application/json','Cache-Control':'no-store'}
    req=Request(base+path,headers=headers,data=None if payload is None else json.dumps(payload).encode())
    with TRANSPORT.open(req,timeout=timeout) as response:return json.load(response)


def plans(case):return schedule(seed=202611100,rate=4,duration=5,rows=1000)


def require(value,message):
    if not value:raise ValueError(message)


class RouteWatch:
    """Every observed non-direct route/error fails; snapshots are not continuous proof."""
    def __init__(self,own,peer):
        self.own,self.peer=own,peer;self.samples=[];self.stop=threading.Event();self.thread=None
    def sample(self):
        row={'monotonic_s':time.perf_counter()}
        try:
            raw=json.loads(subprocess.run(['tailscale','status','--json'],check=True,capture_output=True,text=True,timeout=10).stdout)
            row['network']=select_nodes(raw,self.own,self.peer)
        except Exception as error:row['error_type']=type(error).__name__
        self.samples.append(row)
    def start(self):
        self.sample()
        def run():
            while not self.stop.wait(1):self.sample()
        self.thread=threading.Thread(target=run,daemon=True);self.thread.start();return self
    def finish(self):
        self.stop.set()
        if self.thread:self.thread.join(timeout=12)
        require(not self.thread or not self.thread.is_alive(),'route sampler did not stop')
        self.sample();return self.samples


def verify_routes(samples):
    require(len(samples)>=2,'route window lacks endpoints')
    for row in samples:
        require('error_type' not in row and row['network']['route']=='direct','route window has non-direct/error observation')
        peer=row['network']['peer'];require(peer['CurAddr'] and not peer.get('PeerRelay'),'direct label lacks active endpoint')


def verify_cell(case,target,observer):
    """Replay raw snapshots and writes without a database or cross-host clocks."""
    load=lambda base,name:json.loads((base/name).read_text())
    planned,records,state=load(observer,'schedule.json'),load(observer,'operations.json'),load(target,'validation-state.json')
    require(planned==plans(case) and len(records)==20,'unexpected schedule/record budget')
    oracle=IssueOracle(state);final=oracle.verify_final(state)
    require((state['rows'],state['fanout'],state['query_kind'])==(1000,20,case['query_kind']),'fixture configuration differs')
    for row,expected in zip(records,planned):
        require(all(row.get(k)==v for k,v in expected.items()) and row['outcome']=='completed','scheduled operation incomplete')
        require(row['intended_s']<=row['dispatch_s']<=row['complete_s']<=row['validation_complete_s'],'observer clock order differs')
        if row['kind']=='read':oracle.verify(row['read_evidence'])
    before,after=(load(observer,f'browser-{when}.json')['probes'][0] for when in ('before','after'))
    fault=load(observer,'browser-failure.json')['probes'][0]
    for probe in (before,after):
        require(probe['checks_passed'] and probe['strategy']==case['strategy'],'browser checks incomplete')
        require(math.isfinite(probe['critical_content_paint_ms']) and probe['critical_content_paint_ms']>0,'missing content paint')
        oracle.verify(probe['evidence'])
        entries=[next(e for e in probe['paint_entries'] if e['identifier']==identifier)
                 for identifier in ('issue-count','issue-first-title','issue-first-comments')]
        require(all(math.isfinite(e['renderTime']) and e['renderTime']>0 and e['rect']['width']>0 and e['rect']['height']>0
                    for e in entries),'paint entry lacks visible content')
        require(entries[0]['text']==f'Issues: {probe["evidence"]["issuesCount"]}' and
                entries[1]['text']==probe['evidence']['issues'][0]['title'] and
                entries[2]['text']==f'Comments: {probe["evidence"]["issues"][0]["comment_count"]}', 'painted text differs')
        require(max(e['renderTime'] for e in entries)==probe['critical_content_paint_ms'],'paint arithmetic differs')
        require(probe['api_requests']==int(case['strategy']=='csr'),'browser request count differs')
    require(fault['checks_passed'] and fault['expected_failure'] and fault['strategy']==case['strategy'] and
            fault['app_outcome']=='failed' and fault['api_requests']==int(case['strategy']=='csr'),'browser fault semantics differ')
    failure=load(observer,'logical-failure.json')
    require(failure['outcome']=='failed' and failure['failure_code']=='origin_http_failure' and failure['upstream_status']==503,
            'logical fault semantics differ')
    control=load(target,'visible-control.json');payload=control['mutation']
    require(control['ack']['changed'] is True and control['ack']['operation_id']==payload['operation_id'] and
            control['ack']['revision']==control['before']['revision']+1,'visible control acknowledgement differs')
    prior={**state,'ledger':[r for r in state['ledger'] if r['operation_id']!=payload['operation_id']]}
    require(IssueOracle(prior).issue_at(payload['issue_id'])==control['before'],'visible control prior state differs')
    require(after['evidence']['issues'][0]['id']==control['before']['id'] and
            after['evidence']['issues'][0]['comment_count']==control['before']['comment_count']+1,'changed aggregate not displayed')
    mutations={f'{case["name"]}-{r["id"]}':{'issue_id':r['issue_id'],'kind':r['mutation'],'value':r['value']}
               for r in planned if r['kind']=='write'}
    mutations[payload['operation_id']]={k:payload[k] for k in ('issue_id','kind','value')}
    actual={r['operation_id']:{k:r[k] for k in ('issue_id','kind','value')} for r in state['ledger']}
    require(mutations==actual,'committed mutations differ')
    events=[json.loads(x) for x in (target/'backend.jsonl').read_text().splitlines()]
    expected={f'{case["name"]}-{r["id"]}' for r in planned}|{payload['operation_id'],
        case['name']+'-browser-before',case['name']+'-browser-after',case['name']+'-browser-failure',case['name']+'-logical-fault-0'}
    counts=Counter(e['operation_id'] for e in events)
    require(set(counts)==expected and all(v==1 for v in counts.values()),'backend identities differ')
    require(Counter(e['status'] for e in events)=={200:23,503:2},'backend outcomes differ')
    query_digest=hashlib.sha256((COUNT_SQL+'\n'+QUERIES[case['query_kind']]).encode()).hexdigest()
    for event in events:
        require(event['query_kind']==case['query_kind'],'backend query kind differs')
        if event['path']=='/issues':require(event['read_query_sha256']==query_digest,'backend SQL identity differs')
        if event['operation_id'] in mutations:
            validate_trace(event['backend'],case['query_kind'],mutations[event['operation_id']]['kind'])
    for base in (target,observer):verify_routes(load(base,'routes.json'))
    placement=load(target,'placement.json')
    require(placement['app_cpu']!=placement['db_cpu'],'app and DB logical CPU budgets overlap')
    for sample in placement['samples']:
        for value in sample.values():require(value['status']=='passed','target affinity differs')
    require(len(placement['samples'])>=2,'missing target placement endpoints')
    return {**case,'status':'passed','scheduled_operations':20,'read_snapshots':16,'scheduled_writes':4,
            'control_writes':1,'browser_snapshots':2,'fault_checks':2,'backend_identities':len(expected),'final_oracle':final}


class Target:
    def __init__(self,dsn,output,own,peer_name):
        self.dsn,self.output,self.own,self.peer_name=dsn,output,own,peer_name
        self.stack=None;self.runtime=None;self.case=None;self.index=0;self.finished=False;self.cells=[];self.routes=None
        self.lock=threading.RLock();self.last=time.monotonic();self.remote=None
        self.cases=CASES
        cpus=sorted(os.sched_getaffinity(0));require(len(cpus)>=2,'two target logical CPUs required')
        self.app_cpu,self.db_cpu=cpus[:2]
        require(os.environ.get('QRL_DB_CPU')==str(self.db_cpu),'container DB CPU assignment differs')
    def placement(self):
        r=self.runtime
        return {'origin':affinity_tree(r['origin'].pid,self.app_cpu),
                'web':affinity_tree(r['process'].pid,self.app_cpu),
                'postgres':affinity_tree(int(os.environ['QRL_POSTGRES_HOST_PID']),self.db_cpu)}
    def control(self,name,payload):
        with self.lock:
            self.last=time.monotonic()
            if name=='begin':
                require(self.remote is not None,'observer handshake required')
                require(self.runtime is None and self.index<len(CASES),'case already active/finished')
                case=CASES[self.index];require(payload=={'case':case['name'],'observer':self.remote},'unexpected case/peer')
                directory=self.output/case['name'];directory.mkdir();self.case=case;self.stack=ExitStack()
                self.runtime=self.stack.enter_context(issue_runtime(self.dsn,directory,case['query_kind'],target_cpu=self.app_cpu,
                    prepare_threshold=None,checkpoint=True,write_phase_tracing=True,durable_writes=True))
                diagnostics=self.runtime['origin'].command('diagnostics')
                require(all(diagnostics['durability'][k]=='on' for k in ('synchronous_commit','fsync','full_page_writes')),'durability differs')
                fixture_info=self.runtime['fixture'].diagnostics()
                expected_major=os.environ.get('QRL_EXPECT_POSTGRES_MAJOR')
                if expected_major:require(fixture_info['version'].startswith(f'PostgreSQL {expected_major}.'),'PostgreSQL version differs')
                save(directory/'database.json',{'fixture':fixture_info,'service':diagnostics})
                self.placement_samples=[self.placement()]
                network_state(self.own['overlay_name'],self.peer_name,timeout=30,require_direct=True)
                self.routes=RouteWatch(self.own['overlay_name'],self.peer_name).start()
                return {'case':case,'ready':True}
            require(self.runtime is not None and payload=={'case':self.case['name']},'no matching active case')
            if name in ('fault-on','fault-off'):
                self.runtime['origin'].command('fault',503 if name=='fault-on' else None);return {'ok':True}
            if name=='visible-write':
                before=self.runtime['fixture'].page()['issues'][0]
                mutation={'operation_id':self.case['name']+'-visible-comment','issue_id':before['id'],
                          'kind':'comment','value':'Off-host browser-visible discussion'}
                req=Request(self.runtime['origin'].url+'/control/mutate',data=json.dumps(mutation).encode(),
                    headers={'X-QRL-Control':'engineering','Content-Type':'application/json','X-QRL-Operation':mutation['operation_id']})
                with TRANSPORT.open(req,timeout=5) as response:ack=json.load(response)
                save(self.output/self.case['name']/'visible-control.json',{'mutation':mutation,'ack':ack,'before':before})
                return {'changed':True}
            if name=='end':
                directory=self.output/self.case['name'];self.placement_samples.append(self.placement())
                save(directory/'placement.json',{'app_cpu':self.app_cpu,'db_cpu':self.db_cpu,'samples':self.placement_samples})
                samples=self.routes.finish();self.routes=None;save(directory/'routes.json',samples);verify_routes(samples)
                save(directory/'database-after.json',self.runtime['origin'].command('diagnostics'))
                self.runtime['quiesce']();state=self.runtime['fixture'].validation_state()
                save(directory/'validation-state.json',state);final=IssueOracle(state).verify_final(state)
                self.stack.close();self.stack=None;self.runtime=None;self.cells.append({**self.case,'final_oracle':final})
                self.index+=1;return {'closed':True}
            raise ValueError('unknown control')
    def close(self):
        if self.routes:
            save(self.output/self.case['name']/'routes.json',self.routes.finish());self.routes=None
        if self.stack:self.stack.close();self.stack=None


def gateway(target,bind_ip,peer_ip,*,classify_proxy_transport=False,path_controls=False,persistent_connections=False,force_connection_close=False,port=8765):
    base=BaseHTTPRequestHandler
    if persistent_connections:
        from .persistent_http import PersistentHandler
        base=PersistentHandler
    class Handler(base):
        def log_message(self,*args):pass
        def send(self,status,body,content_type='application/json'):
            if persistent_connections:
                from .persistent_http import observe_send
                return observe_send(self,target,status,body,lambda:self.send_body(status,body,content_type))
            return self.send_body(status,body,content_type)
        def send_body(self,status,body,content_type):
            if persistent_connections and force_connection_close:self.close_connection=True
            self.send_response(status);self.send_header('Content-Type',content_type)
            if persistent_connections:self.send_header('X-QRL-Connection',self.connection_id)
            if persistent_connections and self.close_connection:self.send_header('Connection','close')
            self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','no-store')
            self.end_headers();self.wfile.write(body)
        def handle_request(self):
            if self.client_address[0]!=peer_ip:self.send(403,b'{}');return
            path=urlparse(self.path).path
            try:
                require(not self.headers.get('Origin'),'browser-origin control/proxy requests rejected')
                require(not urlparse(self.path).netloc and not urlparse(self.path).scheme and len(self.path)<=2048,'relative bounded path required')
                body=None
                if self.command=='POST':
                    size=int(self.headers.get('Content-Length','0'));require(0<size<=32768,'body bound')
                    self.connection.settimeout(15);body=self.rfile.read(size);require(len(body)==size,'incomplete body')
                if path_controls and path.startswith('/qrl/path-probe/') and self.command=='GET':
                    from .path_controls import serve
                    serve(self,target,path);return
                if path=='/manifest' and self.command=='GET':value=target.own
                elif path=='/qrl/hello' and self.command=='POST':
                    value=json.loads(body);check_pair(target.own,value)
                    require(target.remote in (None,value),'different observer');target.remote=value;value={'accepted':True}
                elif path=='/qrl/finish' and self.command=='POST':
                    require(json.loads(body)=={'observer':target.remote} and target.index==len(target.cases) and target.runtime is None,'incomplete run')
                    self.send(200,b'{"finished": true}');target.finished=True;return
                elif path.startswith('/qrl/') and self.command=='POST':value=target.control(path[len('/qrl/'):],json.loads(body))
                else:
                    with target.lock:
                        require(target.runtime is not None,'no active application')
                        if self.command=='GET' and (path in ('/health','/issues/ssr','/issues/csr','/api/issues','/static/client.js') or
                                (persistent_connections and path=='/static/ranked_client.js')):
                            url=target.runtime['url']+self.path
                        elif self.command=='POST' and path=='/control/mutate':
                            require(self.headers.get('X-QRL-Control')=='engineering','write control header required')
                            url=target.runtime['origin'].url+path
                        else:self.send(404,b'{}');return
                        case_name=target.case['name'] if classify_proxy_transport else None
                    headers={'Cache-Control':'no-store','X-QRL-Operation':self.headers.get('X-QRL-Operation','')[:128]}
                    if body is not None:headers.update({'Content-Type':'application/json','X-QRL-Control':'engineering'})
                    req=Request(url,data=body,headers=headers)
                    if classify_proxy_transport:
                        from .proxy_transport import forward
                        forward(TRANSPORT,req,self.send,target,case_name,path,self.command,headers['X-QRL-Operation'])
                        return
                    try:
                        with TRANSPORT.open(req,timeout=15) as response:
                            status,data,kind=response.status,response.read(),response.headers.get('Content-Type','application/octet-stream')
                    except HTTPError as error:status,data,kind=error.code,error.read(),error.headers.get('Content-Type','application/json')
                    self.send(status,data,kind);return
                self.send(200,json.dumps(value).encode())
            except Exception as error:
                target.errors.append({'path':path,'error_type':type(error).__name__})
                self.send(400,json.dumps({'error_type':type(error).__name__}).encode())
        do_GET=handle_request
        do_POST=handle_request
    target.errors=[]
    if persistent_connections:target.connection_observations=[]
    if path_controls:target.path_control_events={}
    if classify_proxy_transport:target.proxy_transport_failures=[]
    server_class=ThreadingHTTPServer
    if persistent_connections:
        from .persistent_http import PersistentServer
        server_class=PersistentServer
    server=server_class((bind_ip,port),Handler);server.daemon_threads=True;server.timeout=1
    return server


def run_browser(base,directory,strategy,label):
    config={'app_url':base,'strategy':strategy,'failure':label=='browser-failure','offhost':True,
            'operation_id':directory.name+'-'+label,'report_path':str(directory/(label+'.json')),
            'screenshot_path':str(directory/(label+'.png'))}
    filename=directory/(label+'-config.json');save(filename,config)
    with (directory/(label+'.log')).open('w') as log:
        subprocess.run([shutil.which('node'),str(Path(__file__).with_name('browser_check.cjs')),str(filename)],
            check=True,timeout=40,stdout=log,stderr=subprocess.STDOUT)
    return json.loads(Path(config['report_path']).read_text())


def observer(output,own,peer_name,peer_ip):
    base=f'http://{peer_ip}:8765';deadline=time.monotonic()+180
    while True:
        try:remote=request_json(base,'/manifest',timeout=3);break
        except OSError:
            if time.monotonic()>deadline:raise
            time.sleep(1)
    check_pair(own,remote);request_json(base,'/qrl/hello',own)
    cells=[]
    for case in CASES:
        directory=output/case['name'];directory.mkdir()
        require(request_json(base,'/qrl/begin',{'case':case['name'],'observer':own})=={'case':case,'ready':True},'begin differs')
        network_state(own['overlay_name'],peer_name,timeout=30,require_direct=True)
        watch=RouteWatch(own['overlay_name'],peer_name).start()
        try:
            run_browser(base,directory,case['strategy'],'browser-before')
            request_json(base,'/qrl/fault-on',{'case':case['name']})
            run_browser(base,directory,case['strategy'],'browser-failure')
            failure=operation({'id':0,'kind':'read','project_id':0,'state':'open','limit':20,'offset':0},
                case['strategy'],base,base,case['name']+'-logical-fault',allowed_remote_host=peer_ip)
            save(directory/'logical-failure.json',failure)
            request_json(base,'/qrl/fault-off',{'case':case['name']})
            planned=plans(case);save(directory/'schedule.json',planned)
            records=execute(planned,lambda row:operation(row,case['strategy'],base,base,case['name'],
                            allowed_remote_host=peer_ip),workers=4,drain_timeout=15)
            save(directory/'operations.json',records)
            require(all(r['outcome']=='completed' for r in records),'mixed work incomplete')
            request_json(base,'/qrl/visible-write',{'case':case['name']})
            run_browser(base,directory,case['strategy'],'browser-after')
        finally:
            samples=watch.finish();save(directory/'routes.json',samples)
        verify_routes(samples);request_json(base,'/qrl/end',{'case':case['name']})
        cells.append(case);print(json.dumps({'phase':'completed',**case}),flush=True)
    require(request_json(base,'/qrl/finish',{'observer':own})=={'finished':True},'finish differs')
    return remote,cells


def main():
    import argparse
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--role',choices=('target','observer'),required=True)
    args=parser.parse_args();output=Path('ci-artifacts/issue-offhost');output.mkdir(parents=True,exist_ok=False)
    own=manifest(args.role,os.environ['GITHUB_RUN_ID'],os.environ['GITHUB_RUN_ATTEMPT'],os.environ['GITHUB_SHA'])
    peer_name=node_name(own['run_id'],own['run_attempt'],'observer' if args.role=='target' else 'target')
    report={'role':ROLE,'performance_estimates':False,'local':own,'status':'started','cases':CASES,
            'platform':platform.platform(),'python':platform.python_version(),
            'clock_scope':'Observer response clocks and target backend clocks are separate; no cross-host subtraction.',
            'python_packages':{name:importlib.metadata.version(name) for name in ('Flask','gunicorn','psycopg','psycopg-binary','psutil')},
            'source_sha256':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in
                list((ROOT/'experiments/issue_dashboard').glob('*.py'))+list((ROOT/'experiments/issue_dashboard').glob('*.cjs'))+
                list((ROOT/'experiments/query_rendering_load').glob('*.py'))+list((ROOT/'apps/issue-dashboard').glob('*.py'))+
                list((ROOT/'tests').glob('test_query_rendering*.py'))+list((ROOT/'tests').glob('test_issue_dashboard*.py'))+
                list((ROOT/'apps/issue-dashboard/templates').glob('*.html'))+list((ROOT/'apps/issue-dashboard/static').glob('*.js'))+
                [ROOT/p for p in ('scripts/offhost_connectivity.py','apps/issue-dashboard/requirements.txt',
                  'scripts/audit_issue_offhost.py','tests/test_offhost_connectivity.py',
                  'apps/issue-dashboard/package-lock.json','.github/workflows/query-rendering-offhost-connectivity.yml',
                  'paper/followup/query-rendering-load/protocol/issue_offhost_engineering_v1.md')]}}
    target=server=None
    try:
        state=network_state(own['overlay_name'],peer_name,timeout=300);report['network_discovery']=state
        if args.role=='observer':report['remote'],report['cells']=observer(output,own,peer_name,private_ip(state['peer']['TailscaleIPs']))
        else:
            target=Target(os.environ['QRL_TEST_DATABASE_URL'],output,own,peer_name)
            server=gateway(target,private_ip(state['self']['TailscaleIPs']),private_ip(state['peer']['TailscaleIPs']))
            deadline=time.monotonic()+600
            while not target.finished and time.monotonic()<deadline:
                server.handle_request()
                if time.monotonic()-target.last>180:raise RuntimeError('observer idle timeout')
            require(target.finished and not target.errors,'application protocol incomplete/errors')
            report.update(remote=target.remote,cells=target.cells)
        report['status']='passed'
    except Exception as error:report.update(status='failed',error_type=type(error).__name__);raise
    finally:
        try:
            if server:server.server_close()
            if target:
                report['gateway_errors']=target.errors;target.close()
        except Exception as error:
            report.update(status='failed',cleanup_error_type=type(error).__name__);raise
        finally:save(output/'report.json',report)


if __name__=='__main__':main()
