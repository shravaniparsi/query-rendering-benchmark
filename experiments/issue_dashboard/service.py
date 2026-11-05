"""Separate-process loopback origin for the issue aggregation task."""
import json
import hashlib
import multiprocessing
import os
from pathlib import Path
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs,urlparse

from experiments.query_rendering_load.isolation_runtime import Origin
from experiments.query_rendering_load.service import BoundedHTTPServer,Pool
from .fixture import IssueFixture,COUNT_SQL,QUERIES
from .write_phases import WritePhases
from .cpu_budget import mask


class IssueService:
    def __init__(self,dsn,schema,size=4,prepare_threshold=5,write_phase_tracing=False,durable_writes=False,wal_io_timing=None):
        if type(write_phase_tracing) is not bool or type(durable_writes) is not bool:raise ValueError('boolean write tracing/durability controls required')
        self.write_phase_tracing=write_phase_tracing;self.durable_writes=durable_writes
        if wal_io_timing is not None and type(wal_io_timing) is not bool:raise ValueError('boolean WAL timer control required')
        self.fixture = IssueFixture(dsn,schema)
        self.config = self.fixture.configuration()
        self.query_sha256=hashlib.sha256((COUNT_SQL+'\n'+QUERIES[self.config['query_kind']]).encode()).hexdigest()
        def connect():
            conn = self.fixture.driver.connect(dsn,autocommit=True,application_name='issue:'+schema,
                                               prepare_threshold=prepare_threshold)
            try:
                conn.execute(self.fixture.driver.sql.SQL('SET search_path TO {}, public').format(
                    self.fixture.driver.sql.Identifier(schema)))
                conn.execute("SET statement_timeout TO '10s'")
                if durable_writes:conn.execute("SET synchronous_commit TO 'on'")
                if wal_io_timing is not None:conn.execute("SET track_wal_io_timing TO '"+('on' if wal_io_timing else 'off')+"'")
                return conn
            except Exception:
                conn.close();raise
        self.pool = Pool(connect,size=size,timeout=2)

    def diagnostics(self):
        with self.pool.acquire() as (conn,_):
            return {'query_kind':self.config['query_kind'],'read_query_sha256':self.query_sha256,
                'prepare_threshold':conn.prepare_threshold,'pool_size':self.pool.available.maxsize,
                'backend_pid':conn.execute('SELECT pg_backend_pid()').fetchone()[0],
                'pool_connections':conn.execute('SELECT count(*) FROM pg_stat_activity WHERE application_name=%s',
                    ('issue:'+self.fixture.schema,)).fetchone()[0],
                'statement_timeout':conn.execute("SELECT current_setting('statement_timeout')").fetchone()[0],
                'write_phase_tracing':self.write_phase_tracing,'durable_writes':self.durable_writes,
                'durability':dict(zip(('synchronous_commit','fsync','full_page_writes','wal_sync_method'),
                    conn.execute("SELECT current_setting('synchronous_commit'),current_setting('fsync'),"
                                 "current_setting('full_page_writes'),current_setting('wal_sync_method')").fetchone())),
                'count_plan':conn.execute('EXPLAIN (FORMAT JSON) '+COUNT_SQL,(0,'open')).fetchone()[0],
                'page_plan':conn.execute('EXPLAIN (FORMAT JSON) '+QUERIES[self.config['query_kind']],
                    (0,'open',20,0)).fetchone()[0]}

    def page(self,project_id=0,state='open',limit=20,offset=0):
        with self.pool.acquire() as (conn,wait_ms):
            started = time.perf_counter()
            result = self.fixture.page_on(conn,self.config['query_kind'],project_id,state,limit,offset)
            result['backend'] = {'pool_wait_ms':wait_ms,'transaction_wall_ms':(time.perf_counter()-started)*1000}
            return result

    def mutate(self,**payload):
        with self.pool.acquire() as (conn,wait_ms):
            started = time.perf_counter()
            trace=WritePhases(started) if self.write_phase_tracing else None
            context=conn.transaction()
            if trace:context=trace.transaction(context)
            with context:
                result = self.fixture.write_on(conn,self.config['query_kind'],**payload,phase_call=trace.call if trace else None)
            wall_ms=(time.perf_counter()-started)*1000
            result['backend'] = {'pool_wait_ms':wait_ms,'transaction_wall_ms':wall_ms,'backend_pid':conn.info.backend_pid}
            if trace:result['backend']['write_phase_trace']=trace.finish(wall_ms)
            return result


def make_server(service,trace_path):
    lock = threading.Lock()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass

        def respond(self,status,result):
            request_id = str(uuid.uuid4())
            result['request_id'] = request_id
            if self.path!='/health':
                event = {'operation_id':self.headers.get('X-QRL-Operation','')[:128],'request_id':request_id,
                    'path':urlparse(self.path).path,'status':status,'monotonic_s':time.perf_counter(),
                    'snapshot':result.get('snapshot'),'revision':result.get('revision'),'backend':result.get('backend'),
                    'query_kind':service.config['query_kind'],
                    'read_query_sha256':service.query_sha256 if urlparse(self.path).path=='/issues' else None}
                with lock:
                    with Path(trace_path).open('a') as output:output.write(json.dumps(event)+'\n')
            body = json.dumps(result).encode()
            try:
                self.send_response(status)
                for key,value in (('Content-Type','application/json'),('Cache-Control','no-store'),
                                  ('Content-Length',str(len(body))),('X-Request-ID',request_id)):
                    self.send_header(key,value)
                self.end_headers();self.wfile.write(body)
            except (BrokenPipeError,ConnectionResetError,TimeoutError):
                # The backend status is an attempted response, not proof of client receipt.
                pass

        def do_GET(self):
            parsed = urlparse(self.path)
            try:
                if parsed.path=='/health':
                    self.respond(200,{'status':'engineering'});return
                if parsed.path!='/issues':
                    self.respond(404,{'error':'not found'});return
                if self.server.forced_status is not None:
                    self.respond(self.server.forced_status,{'error':'injected failure'});return
                args = parse_qs(parsed.query)
                if set(args)-{'project_id','state','limit','offset'} or any(len(v)!=1 for v in args.values()):
                    raise ValueError('invalid parameters')
                result = service.page(int(args.get('project_id',['0'])[0]),args.get('state',['open'])[0],
                                      int(args.get('limit',['20'])[0]),int(args.get('offset',['0'])[0]))
                self.respond(200,result)
            except (ValueError,TypeError,OverflowError):self.respond(400,{'error':'invalid issue request'})
            except TimeoutError:self.respond(503,{'error':'pool admission timeout'})
            except Exception:self.respond(500,{'error':'database operation failed'})

        def do_POST(self):
            if self.path!='/control/mutate':
                self.respond(404,{'error':'not found'});return
            if self.headers.get('Origin') or self.headers.get('X-QRL-Control')!='engineering':
                self.respond(403,{'error':'engineering control required'});return
            try:
                length = int(self.headers.get('Content-Length','0'))
                if not 0<length<=32768:raise ValueError('body size')
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload,dict) or set(payload)!={'operation_id','issue_id','kind','value'}:
                    raise ValueError('mutation fields')
                self.respond(200,service.mutate(**payload))
            except (ValueError,TypeError):self.respond(400,{'error':'invalid mutation'})
            except TimeoutError:self.respond(503,{'error':'admission or body timeout'})
            except Exception:self.respond(500,{'error':'database operation failed'})
    server = BoundedHTTPServer(('127.0.0.1',0),Handler,max_handlers=16)
    server.forced_status = None
    return server


def _child(pipe,dsn,schema,trace_path,cpu,pool_size=4,prepare_threshold=5,write_phase_tracing=False,durable_writes=False,wal_io_timing=None,service_factory=None):
    service=server=thread=None
    try:
        if cpu is not None:os.sched_setaffinity(0,set(mask(cpu)))
        factory=IssueService if service_factory is None else service_factory
        service = factory(dsn,schema,size=pool_size,prepare_threshold=prepare_threshold,
                               write_phase_tracing=write_phase_tracing,durable_writes=durable_writes,wal_io_timing=wal_io_timing)
        server = make_server(service,trace_path)
        thread = threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        pipe.send({'status':'ready','pid':os.getpid(),'port':server.server_port})
        while True:
            name,value = pipe.recv()
            if name=='stop':pipe.send({'status':'ok','result':True});break
            if name=='phase':server.phase=value;result=True
            elif name=='status':result=dict(server.admission_rejections)
            elif name=='fault':server.forced_status=value;result=True
            elif name=='quiesce':server.shutdown();result=server.wait_for_idle(15)
            elif name=='diagnostics':result=service.diagnostics()
            elif name=='wal-snapshot':
                from .wal_observer import snapshot
                result=snapshot(service)
            else:raise ValueError('unknown command')
            pipe.send({'status':'ok','result':result})
    except (EOFError,BrokenPipeError):pass
    except Exception as error:
        try:pipe.send({'status':'failed','error_type':type(error).__name__})
        except (EOFError,BrokenPipeError):pass
    finally:
        if server:server.shutdown();server.server_close()
        if thread:thread.join(timeout=5)
        if service:service.pool.close()
        pipe.close()


class IssueOrigin(Origin):
    def __init__(self,dsn,schema,trace_path,cpu=None,pool_size=4,prepare_threshold=5,write_phase_tracing=False,durable_writes=False,wal_io_timing=None,service_factory=None):
        self.placement='separate_process'
        self.server=self.service=self.thread=self.process=self.pipe=None
        try:
            context = multiprocessing.get_context('spawn')
            self.pipe,child = context.Pipe()
            self.process = context.Process(target=_child,args=(child,dsn,schema,trace_path,cpu,pool_size,prepare_threshold,write_phase_tracing,durable_writes,wal_io_timing,service_factory))
            self.process.start();child.close()
            ready = self._receive(30)
            if ready['status']!='ready' or ready['pid']==os.getpid():raise RuntimeError('issue origin isolation failed')
            self.pid=ready['pid'];self.url=f'http://127.0.0.1:{ready["port"]}'
        except Exception:
            self.close();raise
