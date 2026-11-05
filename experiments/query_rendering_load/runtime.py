"""Disposable per-cell application/database fixture for engineering collection."""
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import socket
import subprocess
import threading
import time
import uuid
from urllib.request import urlopen

from .fixture import Fixture
from .service import FeedService, make_server

ROOT = Path(__file__).resolve().parents[2]


@contextmanager
def cell_runtime(dsn, rows, indexed, output, maintenance='analyze_only'):
    fixture = service = server = process = None
    try:
        fixture = Fixture(dsn,'qrl_mix_'+uuid.uuid4().hex)
        fixture.initialize(rows,indexed=indexed)
        fixture.prepare_maintenance(maintenance)
        service = FeedService(dsn,fixture.schema,size=4)
        server = make_server(0,service,max_handlers=16,trace_path=output/'backend.jsonl')
        thread = threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        origin = f'http://127.0.0.1:{server.server_port}'
        with socket.socket() as probe:
            probe.bind(('127.0.0.1',0))
            port = probe.getsockname()[1]
        app = ROOT/'apps/query-rendering-load'
        node = os.environ.get('QRL_NODE',shutil.which('node'))
        url = f'http://127.0.0.1:{port}'
        with (output/'next.log').open('w') as log:
            process = subprocess.Popen([node,str(app/'node_modules/next/dist/bin/next'),'start','-H','127.0.0.1','-p',str(port)],
                cwd=app,env=dict(os.environ,QRL_ORIGIN=origin,NEXT_TELEMETRY_DISABLED='1'),stdout=log,stderr=subprocess.STDOUT)
            deadline = time.monotonic()+30
            while True:
                try:
                    with urlopen(url,timeout=1):
                        break
                except OSError:
                    if process.poll() is not None or time.monotonic()>deadline:
                        raise RuntimeError('application startup failed')
                    time.sleep(.2)
            def quiesce():
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait()
                server.shutdown()
                if not server.wait_for_idle(15):
                    raise RuntimeError('origin did not drain before validation export')
            yield {'fixture':fixture,'service':service,'server':server,'process':process,'url':url,'origin':origin,
                   'quiesce':quiesce}
    finally:
        if process:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait()
        if server:
            server.shutdown(); server.server_close(); thread.join(timeout=5)
        if service:
            service.pool.close()
        if fixture:
            with fixture.connection() as conn:
                conn.execute(fixture.driver.sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(fixture.driver.sql.Identifier(fixture.schema)))
