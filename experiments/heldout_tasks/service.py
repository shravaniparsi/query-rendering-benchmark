"""Separate-process reserved-task origin, initially used for correctness only."""
import json
import multiprocessing
import os
from pathlib import Path
import threading
from urllib.parse import urlsplit, parse_qs
from http.server import BaseHTTPRequestHandler

from .inventory import require
from .inventory_sql import InventorySQL
from .analytics_sql import AnalyticsSQL
from experiments.query_rendering_load.service import BoundedHTTPServer, Pool
from experiments.query_rendering_load.isolation_runtime import Origin


TASKS = {'inventory': InventorySQL, 'analytics': AnalyticsSQL}


class ReservedService:
    def __init__(self, dsn, schema, task, pool_size=4, *, scale=False):
        require(task in TASKS and type(scale) is bool, 'unknown reserved task/fixture'); self.task = task
        if scale:
            from .scale_sql import ScaleInventorySQL, ScaleAnalyticsSQL
            fixture_class = ScaleInventorySQL if task == 'inventory' else ScaleAnalyticsSQL
        else: fixture_class = TASKS[task]
        self.fixture = fixture_class(dsn, schema)
        self.config = self.fixture.configuration()
        def connect():
            connection = self.fixture.driver.connect(dsn, autocommit=True, prepare_threshold=None, application_name='reserved:' + schema)
            try:
                connection.execute(self.fixture.driver.sql.SQL('SET search_path TO {}, public').format(self.fixture.driver.sql.Identifier(schema)))
                connection.execute("SET statement_timeout TO '10s'"); connection.execute("SET synchronous_commit TO 'on'")
                return connection
            except Exception: connection.close(); raise
        self.pool = Pool(connect, size=pool_size, timeout=2)

    def query(self, parameters):
        with self.pool.acquire() as (connection, _): return self.fixture.query_on(connection, **parameters)

    def mutate(self, payload):
        with self.pool.acquire() as (connection, _):
            return (self.fixture.apply_on if self.task == 'inventory' else self.fixture.append_on)(connection, **payload)


def make_server(service, trace_path):
    lock = threading.Lock()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def send(self, status, result):
            body = json.dumps(result).encode()
            if self.path != '/health':
                event = {'operation_id': self.headers.get('X-QRL-Operation', '')[:128], 'path': urlsplit(self.path).path,
                    'status': status, 'task': service.task, 'query_kind': service.config['query_kind'], 'snapshot': result.get('snapshot'),
                    'read_operation': result.get('read_operation'), 'comparative_timing': False}
                with lock:
                    with Path(trace_path).open('a') as stream: stream.write(json.dumps(event) + '\n')
            try:
                self.send_response(status); self.send_header('Content-Type', 'application/json'); self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
            except (OSError, TimeoutError): pass
        def do_GET(self):
            parsed = urlsplit(self.path)
            if parsed.path == '/health': self.send(200, {'ok': True, 'task': service.task}); return
            if parsed.path != '/query': self.send(404, {'ok': False, 'error': 'not_found'}); return
            if self.server.forced_status: self.send(self.server.forced_status, {'ok': False, 'error': 'injected_failure'}); return
            try:
                arguments = parse_qs(parsed.query); require(set(arguments) == {'parameters'} and len(arguments['parameters']) == 1, 'query arguments differ')
                parameters = json.loads(arguments['parameters'][0]); require(isinstance(parameters, dict), 'object parameters required')
                result = service.query(parameters); self.send(200, {'ok': True, **result})
            except (ValueError, TypeError, OverflowError): self.send(400, {'ok': False, 'error': 'invalid_query'})
            except TimeoutError: self.send(503, {'ok': False, 'error': 'admission_timeout'})
            except Exception: self.send(500, {'ok': False, 'error': 'database_failure'})
        def do_POST(self):
            if self.path != '/control/mutate': self.send(404, {'ok': False, 'error': 'not_found'}); return
            if self.headers.get('Origin') or self.headers.get('X-QRL-Control') != 'engineering': self.send(403, {'ok': False, 'error': 'control_required'}); return
            if self.server.forced_status: self.send(self.server.forced_status, {'ok': False, 'error': 'injected_failure'}); return
            try:
                size = int(self.headers.get('Content-Length', 0)); require(0 < size <= 32768, 'body bound')
                payload = json.loads(self.rfile.read(size)); require(isinstance(payload, dict), 'object payload required')
                self.send(200, {'ok': True, **service.mutate(payload)})
            except (ValueError, TypeError, OverflowError): self.send(400, {'ok': False, 'error': 'invalid_mutation'})
            except TimeoutError: self.send(503, {'ok': False, 'error': 'admission_timeout'})
            except Exception: self.send(500, {'ok': False, 'error': 'database_failure'})
    server = BoundedHTTPServer(('127.0.0.1', 0), Handler, max_handlers=16); server.forced_status = None; return server


def child(pipe, dsn, schema, task, trace_path, cpu, scale=False):
    service = server = thread = None
    try:
        if cpu is not None: os.sched_setaffinity(0, set(cpu))
        service = ReservedService(dsn, schema, task, scale=scale); server = make_server(service, trace_path)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        pipe.send({'status': 'ready', 'pid': os.getpid(), 'port': server.server_port})
        while True:
            name, value = pipe.recv()
            if name == 'stop': pipe.send({'status': 'ok', 'result': True}); break
            if name == 'fault': server.forced_status = value; result = True
            elif name == 'quiesce': server.shutdown(); result = server.wait_for_idle(15)
            else: raise ValueError('unknown reserved origin command')
            pipe.send({'status': 'ok', 'result': result})
    except (EOFError, BrokenPipeError): pass
    except Exception as error:
        try: pipe.send({'status': 'failed', 'error_type': type(error).__name__})
        except (EOFError, BrokenPipeError): pass
    finally:
        if server: server.shutdown(); server.server_close()
        if thread: thread.join(timeout=5)
        if service: service.pool.close()
        pipe.close()


class ReservedOrigin(Origin):
    def __init__(self, dsn, schema, task, trace_path, cpu=None, *, scale=False):
        self.placement = 'separate_process'; self.server = self.service = self.thread = self.process = self.pipe = None
        try:
            context = multiprocessing.get_context('spawn'); self.pipe, other = context.Pipe()
            self.process = context.Process(target=child, args=(other, dsn, schema, task, trace_path, cpu, scale)); self.process.start(); other.close()
            value = self._receive(30); require(value['status'] == 'ready' and value['pid'] != os.getpid(), 'reserved origin isolation failed')
            self.pid = value['pid']; self.url = f'http://127.0.0.1:{value["port"]}'
        except Exception: self.close(); raise
