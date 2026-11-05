"""Process-placement intervention. The frozen pilot runtime is left intact."""
from contextlib import contextmanager
import multiprocessing
import os
import shutil
import socket
import subprocess
import threading
import time
import uuid
from urllib.request import urlopen

from .fixture import Fixture
from .runtime import ROOT
from .service import FeedService, make_server
from .telemetry import Telemetry


def _origin_child(pipe, dsn, schema, trace_path, pool_size=4, cpu=None):
    service = server = thread = None
    try:
        if cpu is not None:
            os.sched_setaffinity(0, {cpu})
        service = FeedService(dsn, schema, size=pool_size)
        server = make_server(0, service, max_handlers=16, trace_path=trace_path)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        pipe.send({'status': 'ready', 'pid': os.getpid(), 'port': server.server_port})
        while True:
            command, value = pipe.recv()
            if command == 'phase':
                server.phase = value
                result = True
            elif command == 'status':
                result = dict(server.admission_rejections)
            elif command == 'fault':
                server.forced_articles_status = value
                result = True
            elif command == 'quiesce':
                server.shutdown()
                result = server.wait_for_idle(15)
            elif command == 'stop':
                pipe.send({'status': 'ok', 'result': True})
                break
            else:
                raise ValueError('unknown origin control command')
            pipe.send({'status': 'ok', 'result': result})
    except (EOFError, BrokenPipeError):
        pass
    except Exception as error:
        # Driver messages can contain connection information.
        try:
            pipe.send({'status': 'failed', 'error_type': type(error).__name__})
        except (EOFError, BrokenPipeError):
            pass
    finally:
        if server:
            server.shutdown()
            server.server_close()
        if thread:
            thread.join(timeout=5)
        if service:
            service.pool.close()
        pipe.close()


class Origin:
    """Same service/budgets in either one interpreter or a spawned interpreter.

    Control uses a pipe outside measurement, never an extra HTTP control request.
    """
    def __init__(self, dsn, schema, trace_path, placement, pool_size=4, cpu=None):
        if placement not in ('shared_process', 'separate_process'):
            raise ValueError('unknown process placement')
        if type(pool_size) is not int or not 1 <= pool_size <= 64:
            raise ValueError('invalid pool size')
        if cpu is not None and (type(cpu) is not int or cpu < 0 or placement != 'separate_process'):
            raise ValueError('CPU binding requires a separate origin process')
        self.placement = placement
        self.process = self.pipe = self.server = self.service = self.thread = None
        try:
            if placement == 'separate_process':
                context = multiprocessing.get_context('spawn')
                self.pipe, child = context.Pipe()
                self.process = context.Process(target=_origin_child, args=(child, dsn, schema, trace_path, pool_size, cpu))
                self.process.start()
                child.close()
                ready = self._receive(30)
                if ready.get('status') != 'ready' or ready.get('pid') == os.getpid():
                    raise RuntimeError('origin process isolation failed')
                self.pid, port = ready['pid'], ready['port']
            else:
                self.service = FeedService(dsn, schema, size=pool_size)
                self.server = make_server(0, self.service, max_handlers=16, trace_path=trace_path)
                self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
                self.thread.start()
                self.pid, port = os.getpid(), self.server.server_port
            self.url = f'http://127.0.0.1:{port}'
        except Exception:
            self.close()
            raise

    def _receive(self, timeout=25):
        if not self.pipe.poll(timeout):
            raise RuntimeError('origin control timed out')
        value = self.pipe.recv()
        if value.get('status') == 'failed':
            raise RuntimeError('origin child failed: '+value['error_type'])
        return value

    def command(self, name, value=None):
        if self.process:
            if not self.process.is_alive():
                raise RuntimeError('origin process exited')
            self.pipe.send((name, value))
            result = self._receive()['result']
        elif name == 'phase':
            self.server.phase = value
            result = True
        elif name == 'status':
            result = dict(self.server.admission_rejections)
        elif name == 'fault':
            self.server.forced_articles_status = value
            result = True
        elif name == 'quiesce':
            self.server.shutdown()
            result = self.server.wait_for_idle(15)
        else:
            raise ValueError('unknown origin control command')
        if name == 'quiesce' and not result:
            raise RuntimeError('origin handlers did not drain')
        return result

    def close(self):
        if self.process:
            if self.process.is_alive():
                try:
                    self.command('stop')
                except (RuntimeError, EOFError, BrokenPipeError):
                    pass
                self.process.join(timeout=20)
            if self.process.is_alive():
                self.process.terminate()
                self.process.join(timeout=5)
            if self.process.is_alive():
                self.process.kill()
                self.process.join(timeout=5)
            self.pipe.close()
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        if self.thread:
            self.thread.join(timeout=5)
        if self.service:
            self.service.pool.close()


def stop_process(process):
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@contextmanager
def isolation_cell(dsn, rows, indexed, output, placement, *, pool_size=4, target_cpu=None):
    fixture = origin = process = None
    try:
        fixture = Fixture(dsn, 'qrl_iso_'+uuid.uuid4().hex)
        fixture.initialize(rows, indexed=indexed)
        fixture.prepare_maintenance('vacuum_analyze')
        origin = Origin(dsn, fixture.schema, output/'backend.jsonl', placement, pool_size=pool_size, cpu=target_cpu)
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        app = ROOT/'apps/query-rendering-load'
        node = os.environ.get('QRL_NODE', shutil.which('node'))
        url = f'http://127.0.0.1:{port}'
        with (output/'next.log').open('w') as log:
            prefix = ['taskset', '-c', str(target_cpu)] if target_cpu is not None else []
            process = subprocess.Popen(prefix+[node, str(app/'node_modules/next/dist/bin/next'), 'start', '-H',
                '127.0.0.1', '-p', str(port)], cwd=app,
                env=dict(os.environ, QRL_ORIGIN=origin.url, NEXT_TELEMETRY_DISABLED='1'),
                stdout=log, stderr=subprocess.STDOUT)
            deadline = time.monotonic()+30
            while True:
                try:
                    with urlopen(url, timeout=1):
                        break
                except OSError:
                    if process.poll() is not None or time.monotonic()>deadline:
                        raise RuntimeError('application startup failed')
                    time.sleep(.2)
            def quiesce():
                stop_process(process)
                origin.command('quiesce')
            yield {'fixture': fixture, 'origin': origin, 'process': process, 'url': url,
                   'quiesce': quiesce, 'placement': placement}
    finally:
        stop_process(process)
        if origin:
            origin.close()
        if fixture:
            with fixture.connection() as conn:
                conn.execute(fixture.driver.sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(
                    fixture.driver.sql.Identifier(fixture.schema)))


class IsolationTelemetry(Telemetry):
    def __init__(self, fixture, node_pid, origin, postgres_pid=None, browser_pid=None):
        super().__init__(fixture, node_pid, postgres_pid)
        self.groups.pop('python_service_generator_sampler')
        label = 'python_generator_sampler' if origin.placement == 'separate_process' else 'python_service_generator_sampler'
        self.groups[label] = (os.getpid(), False)
        if origin.placement == 'separate_process':
            self.groups['python_origin'] = (origin.pid, False)
        if browser_pid is not None:
            self.groups['browser_driver_tree'] = (browser_pid, True)

    def finish(self):
        value = super().finish()
        value['scope'] = ('Process groups reflect recorded placement. Separate origin and generator have distinct '
            'interpreters but share host CPU, memory and I/O with Next, PostgreSQL and optional Chromium. '
            'Shared-process origin CPU is inseparable from generator/sampler CPU. WAL is cluster-wide, includes '
            'ledger/background work; database stats can lag and include monitoring. Matched-process CPU excludes '
            'unseen short-lived processes. RSS can double-count shared pages; I/O may be unavailable. '
            'Sparse wait samples do not establish absence of contention.')
        return value
