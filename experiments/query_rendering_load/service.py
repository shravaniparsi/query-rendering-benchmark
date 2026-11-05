"""Loopback engineering HTTP service with a bounded persistent connection pool."""
import argparse
import json
import os
import queue
import threading
import time
import uuid
from collections import Counter
from pathlib import Path
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .fixture import COUNT_SQL, PAGE_SQL, Fixture, validate_page


class Pool:
    def __init__(self, factory, size=4, timeout=2):
        if type(size) is not int or not 1 <= size <= 64 or timeout <= 0:
            raise ValueError('invalid pool settings')
        self.available = queue.Queue(size)
        self.factory = factory
        self.timeout = timeout
        self.connections = []
        try:
            for _ in range(size):
                connection = factory()
                self.connections.append(connection)
                self.available.put(connection)
        except Exception:
            self.close()
            raise

    @contextmanager
    def acquire(self):
        start = time.perf_counter()
        try:
            connection = self.available.get(timeout=self.timeout)
        except queue.Empty:
            raise TimeoutError('database pool admission timeout') from None
        try:
            if connection is None or getattr(connection, 'closed', False):
                connection = None
                connection = self.factory()
                self.connections.append(connection)
            yield connection, (time.perf_counter() - start) * 1000
        finally:
            self.available.put(None if getattr(connection, 'closed', False) else connection)

    def close(self):
        for connection in self.connections:
            connection.close()


class FeedService:
    def __init__(self, dsn, schema, size=4, timeout=2):
        self.fixture = Fixture(dsn, schema)
        from psycopg.rows import dict_row
        self.dict_row = dict_row
        def connect():
            conn = self.fixture.driver.connect(dsn, autocommit=True, application_name='qrl:'+schema)
            try:
                conn.execute(self.fixture.driver.sql.SQL('SET search_path TO {}, public').format(
                    self.fixture.driver.sql.Identifier(schema)))
                # Bound abandoned queries independently of HTTP client lifetime.
                conn.execute("SET statement_timeout TO '10s'")
                return conn
            except Exception:
                conn.close()
                raise
        self.pool = Pool(connect, size, timeout)

    def page(self, tag='rendering', limit=20, offset=0):
        validate_page(tag, limit, offset)
        with self.pool.acquire() as (conn, wait_ms):
            started = time.perf_counter()
            with conn.transaction():
                conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
                with conn.cursor(row_factory=self.dict_row) as cursor:
                    cursor.execute('SELECT pg_current_snapshot()::text AS snapshot')
                    snapshot = cursor.fetchone()['snapshot']
                    cursor.execute(COUNT_SQL, (tag,))
                    count = cursor.fetchone()['count']
                    cursor.execute(PAGE_SQL, (tag, limit, offset))
                    rows = cursor.fetchall()
            return {'articles': rows, 'articlesCount': count, 'snapshot': snapshot,
                    'backend': {'pool_wait_ms': wait_ms,
                                'transaction_wall_ms': (time.perf_counter()-started)*1000}}

    def update(self, operation_id, article_id, field, value):
        with self.pool.acquire() as (conn, wait_ms):
            start = time.perf_counter()
            with conn.transaction():
                result = self.fixture.update_on(conn, operation_id, article_id, field, value)
            result['backend'] = {'pool_wait_ms': wait_ms,
                                 'transaction_wall_ms': (time.perf_counter()-start)*1000}
            return result


class BoundedHTTPServer(ThreadingHTTPServer):
    """Reject excess connections before creating another handler thread."""
    def __init__(self, address, handler, max_handlers=16):
        self.slots = threading.BoundedSemaphore(max_handlers)
        self.admission_lock = threading.Lock()
        self.handler_idle = threading.Condition(self.admission_lock)
        self.active_handlers = 0
        self.admission_rejections = Counter()
        self.phase = 'unassigned'
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            with self.admission_lock:
                self.admission_rejections[self.phase] += 1
            try:
                request.settimeout(1)
                request.sendall(b'HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
            finally:
                self.shutdown_request(request)
            return
        try:
            with self.handler_idle:
                self.active_handlers += 1
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            with self.handler_idle:
                self.active_handlers -= 1
                self.handler_idle.notify_all()
            raise

    def process_request_thread(self, request, client_address):
        try:
            request.settimeout(5)
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()
            with self.handler_idle:
                self.active_handlers -= 1
                self.handler_idle.notify_all()

    def wait_for_idle(self, timeout):
        with self.handler_idle:
            return self.handler_idle.wait_for(lambda:self.active_handlers==0,timeout)


def make_server(port, service, max_handlers=16, trace_path=None):
    trace_lock = threading.Lock()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            if self.path != '/control/update':
                self.respond(404, {'error': 'not found'})
                return
            if self.headers.get('Origin') or self.headers.get('X-QRL-Control') != 'engineering':
                self.respond(403, {'error': 'engineering control required'})
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 4096:
                    raise ValueError('invalid body size')
                self.connection.settimeout(5)
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict) or set(payload) != {'operation_id', 'article_id', 'field', 'value'}:
                    raise ValueError('invalid write fields')
                self.respond(200, service.update(**payload))
            except (ValueError, TypeError):
                self.respond(400, {'error': 'invalid update'})
            except TimeoutError:
                self.respond(503, {'error': 'admission or body timeout'})
            except Exception:
                self.respond(500, {'error': 'database operation failed'})

        def respond(self, status, value, request_id=None):
            request_id = request_id or str(uuid.uuid4())
            value['request_id'] = request_id
            if trace_path is not None and getattr(self.server, 'trace_enabled', True) and self.path != '/health':
                event = {'request_id': request_id, 'operation_id': self.headers.get('X-QRL-Operation', '')[:128],
                         'path': urlparse(self.path).path, 'status': status,
                         'monotonic_s': time.perf_counter(), 'backend': value.get('backend'),
                         'revision': value.get('revision'), 'snapshot': value.get('snapshot')}
                with trace_lock:
                    with Path(trace_path).open('a') as trace_file:
                        trace_file.write(json.dumps(event)+'\n')
            body = json.dumps(value).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Request-ID', request_id)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            request_id = str(uuid.uuid4())
            path = urlparse(self.path)
            try:
                if path.path == '/health':
                    status, value = 200, {'status': 'engineering', 'timing_validated': False}
                elif path.path == '/articles':
                    if getattr(self.server, 'forced_articles_status', None) is not None:
                        self.respond(self.server.forced_articles_status, {'error':'engineering injected failure'}, request_id)
                        return
                    args = parse_qs(path.query)
                    if set(args) - {'tag', 'limit', 'offset'} or any(len(v) != 1 for v in args.values()):
                        raise ValueError('invalid query parameters')
                    value = service.page(args.get('tag', ['rendering'])[0],
                                         int(args.get('limit', ['20'])[0]),
                                         int(args.get('offset', ['0'])[0]))
                    status = 200
                else:
                    status, value = 404, {'error': 'not found'}
            except (ValueError, OverflowError):
                status, value = 400, {'error': 'invalid page request'}
            except TimeoutError:
                status, value = 503, {'error': 'pool admission timeout'}
            except Exception:
                # Never return driver exceptions containing connection information.
                status, value = 500, {'error': 'database operation failed'}
            self.respond(status, value, request_id)
    return BoundedHTTPServer(('127.0.0.1', port), Handler, max_handlers)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--schema', required=True)
    parser.add_argument('--port', type=int, default=3210)
    parser.add_argument('--pool-size', type=int, default=4)
    args = parser.parse_args()
    if not os.environ.get('QRL_DATABASE_URL'):
        parser.error('set QRL_DATABASE_URL')
    service = FeedService(os.environ['QRL_DATABASE_URL'], args.schema, args.pool_size)
    server = make_server(args.port, service)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.pool.close()


if __name__ == '__main__':
    main()
