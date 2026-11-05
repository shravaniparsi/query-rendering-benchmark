"""Release-gated reserved origin with native whole-transaction resource evidence."""
from contextlib import ExitStack
import hashlib
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import threading
import time
from urllib.parse import parse_qs,urlsplit
import uuid

from .inventory import require
from .service import ReservedService
from experiments.query_rendering_load.service import BoundedHTTPServer
from experiments.study_design.performance_release import PerformancePermission


def sha(raw):return hashlib.sha256(raw).hexdigest()


class SQLCalls:
    def __init__(self,connection,calls):self.connection=connection;self.calls=calls
    def execute(self,query,parameters=None,*args,**kwargs):
        self.calls.append((query,parameters));return self.connection.execute(query,parameters,*args,**kwargs)
    def cursor(self,*args,**kwargs):return SQLCursor(self.connection.cursor(*args,**kwargs),self.calls)
    def __getattr__(self,name):return getattr(self.connection,name)


class SQLCursor:
    def __init__(self,cursor,calls):self.cursor=cursor;self.calls=calls
    def __enter__(self):self.cursor.__enter__();return self
    def __exit__(self,*args):return self.cursor.__exit__(*args)
    def execute(self,query,parameters=None,*args,**kwargs):
        self.calls.append((query,parameters));self.cursor.execute(query,parameters,*args,**kwargs);return self
    def __getattr__(self,name):return getattr(self.cursor,name)


class PerformanceService(ReservedService):
    def __init__(self,dsn,schema,task,permission,case):
        require(isinstance(permission,PerformancePermission),'actual frozen-artifact permission required')
        self.authorization=permission.permit(task,case['phase'],case['rows'],case['candidate'],case['strategy'],case['collection_date_utc'],context_id=case.get('context_id'))
        require(sha(Path(__file__).read_bytes())==permission._source_sha['experiments/heldout_tasks/performance_service.py'],'executing native service differs from frozen source')
        super().__init__(dsn,schema,task,pool_size=4,scale=True)
        require(self.config['query_kind']==case['candidate'] and self.config['seed']['rows']==case['rows'],'actual SQL fixture differs from released case')
        self.local=threading.local();self.catalog={};self.catalog_lock=threading.Lock()
        original=self.pool.factory
        def configured():
            connection=original()
            try:configure(connection);return connection
            except Exception:connection.close();raise
        def configure(connection):
            connection.execute("SET track_wal_io_timing TO 'off'");connection.execute("SET track_io_timing TO 'off'")
        self.pool.factory=configured
        for connection in self.pool.connections:configure(connection)

    def reset(self):self.local.backend=None
    def backend(self):return getattr(self.local,'backend',None)

    def call(self,function):
        self.reset()
        with self.pool.acquire() as (connection,wait_ms):
            calls=[];proxy=SQLCalls(connection,calls);start=time.perf_counter_ns()
            try:result=function(proxy)
            finally:
                finish=time.perf_counter_ns();statements=[]
                for statement,parameters in calls:
                    text=statement.as_string(connection) if hasattr(statement,'as_string') else str(statement)
                    digest=sha(text.encode());parameter_digest=sha(json.dumps(parameters,sort_keys=True,separators=(',',':'),default=str).encode())
                    with self.catalog_lock:
                        require(digest not in self.catalog or self.catalog[digest]==text,'SQL digest collision');self.catalog[digest]=text
                    statements.append({'sql_sha256':digest,'parameters_sha256':parameter_digest})
                self.local.backend={'pool_wait_ms':wait_ms,'transaction_wall_ms':(finish-start)/1e6,'backend_pid':connection.info.backend_pid,
                    'start_monotonic_ns':str(start),'finish_monotonic_ns':str(finish),'allowed_cpus':sorted(os.sched_getaffinity(0)) if hasattr(os,'sched_getaffinity') else None,
                    'explicit_execute_calls':statements}
            result['backend']={key:self.local.backend[key] for key in ('pool_wait_ms','transaction_wall_ms','backend_pid')};return result

    def query(self,parameters):return self.call(lambda connection:self.fixture.query_on(connection,**parameters))
    def mutate(self,payload):
        require(self.authorization['phase']!='qualification','read-only observation qualification required')
        return self.call(lambda connection:(self.fixture.apply_on if self.task=='inventory' else self.fixture.append_on)(connection,**payload))

    def diagnostics(self):
        settings=[]
        with ExitStack() as stack:
            connections=[stack.enter_context(self.pool.acquire())[0] for _ in range(4)]
            for connection in connections:
                values=connection.execute("SELECT current_setting('synchronous_commit'),current_setting('fsync'),current_setting('full_page_writes'),current_setting('track_wal_io_timing'),current_setting('track_io_timing'),current_setting('statement_timeout'),current_setting('application_name'),version()").fetchone()
                record=dict(zip(('synchronous_commit','fsync','full_page_writes','track_wal_io_timing','track_io_timing','statement_timeout','application_name','postgres'),values))
                require(all(record[key]=='on' for key in ('synchronous_commit','fsync','full_page_writes')) and record['track_wal_io_timing']==record['track_io_timing']=='off' and record['statement_timeout']=='10s' and
                    record['application_name']=='reserved:'+self.fixture.schema and record['postgres'].startswith('PostgreSQL 18.0') and connection.prepare_threshold is None,'native pool/durability/version settings differ')
                settings.append({'pid':connection.info.backend_pid,**record})
        with self.catalog_lock:catalog=dict(sorted(self.catalog.items()))
        return {'task':self.task,'query_kind':self.config['query_kind'],'seed':self.config['seed'],'pool_size':4,'prepare_threshold':None,'backends':sorted(settings,key=lambda r:r['pid']),
            'authorization':self.authorization,'explicit_sql_catalog':catalog,'clock_scope':'Whole SQL/decode/transaction-exit elapsed time; pool wait separate; SQL digest construction follows the timed interval.'}


def wal_snapshot(service):
    """Owned PostgreSQL 18 counters, with reserved-session identity checked."""
    with ExitStack() as stack:
        connections=[stack.enter_context(service.pool.acquire())[0] for _ in range(4)];pids=sorted(c.info.backend_pid for c in connections);settings=[]
        require(len(set(pids))==4,'four distinct native pool backends required')
        for connection in connections:
            connection.execute('SELECT pg_stat_force_next_flush()').fetchone()
            settings.append({'pid':connection.info.backend_pid,'track_wal_io_timing':connection.execute("SELECT current_setting('track_wal_io_timing')").fetchone()[0]})
        with service.fixture.driver.connect(service.fixture.dsn,autocommit=True,application_name='qrl_reserved_wal_observer') as connection:
            require(int(connection.execute("SELECT current_setting('server_version_num')").fetchone()[0])==180000,'pinned PostgreSQL 18.0 per-backend statistics required');connection.execute("SET stats_fetch_consistency TO 'none'");backends=[]
            for pid in pids:
                identity=connection.execute('SELECT pid,backend_start::text,application_name FROM pg_stat_activity WHERE pid=%s',(pid,)).fetchone()
                require(identity is not None and identity[2]=='reserved:'+service.fixture.schema,'reserved pool backend identity differs')
                wal=connection.execute('SELECT to_jsonb(s) FROM pg_stat_get_backend_wal(%s) s',(pid,)).fetchone()[0]
                io=[r[0] for r in connection.execute("SELECT to_jsonb(s) FROM pg_stat_get_backend_io(%s) s WHERE object='wal' ORDER BY context",(pid,))]
                backends.append({'pid':pid,'backend_start':identity[1],'application_name':identity[2],'wal':wal,'io':io})
            cluster=connection.execute('SELECT to_jsonb(s) FROM pg_stat_wal s').fetchone()[0];cluster_io=[r[0] for r in connection.execute("SELECT to_jsonb(s) FROM pg_stat_io s WHERE object='wal' ORDER BY backend_type,context")]
        return {'format':'qrl-pool-wal-v1','settings':sorted(settings,key=lambda r:r['pid']),'backends':backends,'cluster_wal':cluster,'cluster_io':cluster_io,
            'scope':'Pool-owned window counters; group commit/background activity prevents isolated per-request maintenance or fsync attribution.'}


def make_server(service,trace_path):
    lock=threading.Lock()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def send(self,status,result):
            result={**result,'request_id':uuid.uuid4().hex};body=json.dumps(result).encode()
            if self.path!='/health':
                event={'operation_id':self.headers.get('X-QRL-Operation','')[:128],'request_id':result['request_id'],'path':urlsplit(self.path).path,'status':status,
                    'task':service.task,'query_kind':service.config['query_kind'],'snapshot':result.get('snapshot'),'read_operation':result.get('read_operation'),
                    'backend':service.backend(),'monotonic_ns':str(time.perf_counter_ns()),'allowed_cpus':sorted(os.sched_getaffinity(0)) if hasattr(os,'sched_getaffinity') else None,
                    'body_bytes':len(body),'body_sha256':sha(body),'authorization':service.authorization}
                with lock:
                    with Path(trace_path).open('a') as stream:stream.write(json.dumps(event)+'\n')
            try:
                self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
            except (OSError,TimeoutError):pass
        def do_GET(self):
            service.reset();parsed=urlsplit(self.path)
            if parsed.path=='/health':self.send(200,{'ok':True,'task':service.task});return
            if parsed.path!='/query':self.send(404,{'ok':False,'error':'not_found'});return
            if self.server.forced_status:self.send(self.server.forced_status,{'ok':False,'error':'injected_failure'});return
            try:
                args=parse_qs(parsed.query);require(set(args)=={'parameters'} and len(args['parameters'])==1,'bounded task query required');parameters=json.loads(args['parameters'][0]);require(isinstance(parameters,dict),'object parameters required')
                self.send(200,{'ok':True,**service.query(parameters)})
            except (ValueError,TypeError,OverflowError):self.send(400,{'ok':False,'error':'invalid_query'})
            except TimeoutError:self.send(503,{'ok':False,'error':'admission_timeout'})
            except Exception:self.send(500,{'ok':False,'error':'database_failure'})
        def do_POST(self):
            service.reset()
            if self.path!='/control/mutate':self.send(404,{'ok':False,'error':'not_found'});return
            if self.headers.get('Origin') or self.headers.get('X-QRL-Control')!='engineering':self.send(403,{'ok':False,'error':'control_required'});return
            if self.server.forced_status:self.send(self.server.forced_status,{'ok':False,'error':'injected_failure'});return
            try:
                size=int(self.headers.get('Content-Length',0));require(0<size<=32768,'bounded mutation required');payload=json.loads(self.rfile.read(size));require(isinstance(payload,dict),'object payload required')
                self.send(200,{'ok':True,**service.mutate(payload)})
            except (ValueError,TypeError,OverflowError):self.send(400,{'ok':False,'error':'invalid_mutation'})
            except TimeoutError:self.send(503,{'ok':False,'error':'admission_timeout'})
            except Exception:self.send(500,{'ok':False,'error':'database_failure'})
    server=BoundedHTTPServer(('127.0.0.1',0),Handler,max_handlers=16);server.forced_status=None;return server
