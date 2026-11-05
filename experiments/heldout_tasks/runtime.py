"""Disposable SQL/origin/WSGI applications for reserved-task correctness only."""
from contextlib import contextmanager
import ipaddress
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid
from urllib.request import urlopen
from urllib.parse import urlsplit

from .inventory import require
from .inventory_sql import InventorySQL
from .analytics_sql import AnalyticsSQL
from .service import ReservedOrigin
from experiments.query_rendering_load.isolation_runtime import stop_process

ROOT = Path(__file__).resolve().parents[2]


def seed_correctness(fixture, task, kind):
    if task == 'inventory':
        products = [{'id': i, 'category': 'category-' + str(i % 4), 'brand': 'brand-' + str(i % 3),
            'price_minor': 100 + i * 37, 'initial_stock': 3 if i == 1 else i % 5} for i in range(1, 31)]
        fixture.initialize(products, kind)
    else:
        fixture.initialize(kind)
        for index in range(12): fixture.append('seed-sale-' + str(index), 'sale', 100 + index, 1000 + index,
            region='region-' + str(index % 3), category='category-' + str(index % 2))


@contextmanager
def reserved_runtime(dsn, output, task, kind, *, purpose='correctness', target_cpu=None, scale_rows=None, public_origin=None):
    require(purpose == 'correctness', 'Reserved performance requires a later frozen-artifact release path')
    require(task in ('inventory', 'analytics') and kind in ('correlated', 'batched', 'maintained'), 'reserved task/query identity differs')
    if public_origin is not None:
        endpoint = urlsplit(public_origin); address = ipaddress.ip_address(endpoint.hostname)
        require(endpoint.scheme == 'http' and endpoint.port and not endpoint.path and not endpoint.query and not endpoint.fragment and
            endpoint.username is None and address.version == 4 and (address.is_loopback or address in ipaddress.ip_network('100.64.0.0/10')), 'literal research public origin required')
    fixture = origin = process = None; output = Path(output); output.mkdir(parents=True, exist_ok=True)
    try:
        if scale_rows is None: cls = InventorySQL if task == 'inventory' else AnalyticsSQL
        else:
            from .scale_sql import ScaleInventorySQL, ScaleAnalyticsSQL, size
            size(scale_rows); cls = ScaleInventorySQL if task == 'inventory' else ScaleAnalyticsSQL
        fixture = cls(dsn, 'qrl_reserved_' + uuid.uuid4().hex)
        if scale_rows is None: seed_correctness(fixture, task, kind)
        else: fixture.initialize_scale(scale_rows, kind)
        origin = ReservedOrigin(dsn, fixture.schema, task, output / 'backend.jsonl', target_cpu, scale=scale_rows is not None)
        with socket.socket() as probe: probe.bind(('127.0.0.1', 0)); port = probe.getsockname()[1]
        url = f'http://127.0.0.1:{port}'
        prefix = ['taskset', '-c', ','.join(map(str, target_cpu))] if target_cpu is not None else []
        with (output / 'web.log').open('w') as log:
            process = subprocess.Popen(prefix + [sys.executable, '-m', 'gunicorn', '--bind', f'127.0.0.1:{port}',
                '--worker-class', 'gthread', '--workers', '1', '--threads', '8', '--timeout', '15', '--graceful-timeout', '5',
                '--access-logfile', '-', '--error-logfile', '-', 'web:app'], cwd=ROOT / 'apps/reserved-tasks',
                env=dict(os.environ, RESERVED_ORIGIN=origin.url, RESERVED_TASK=task,
                    **({'RESERVED_PUBLIC_ORIGIN': public_origin} if public_origin is not None else {})), stdout=log, stderr=subprocess.STDOUT)
            deadline = time.monotonic() + 30
            while True:
                try:
                    with urlopen(url + '/health', timeout=1): break
                except OSError:
                    if process.poll() is not None or time.monotonic() > deadline: raise RuntimeError('reserved WSGI startup failed')
                    time.sleep(.1)
            def quiesce(): stop_process(process); origin.command('quiesce')
            yield {'fixture': fixture, 'origin': origin, 'process': process, 'url': url, 'quiesce': quiesce,
                'task': task, 'query_kind': kind, 'purpose': purpose, 'scale_rows': scale_rows, 'comparative_timing': False}
    finally:
        stop_process(process)
        if origin: origin.close()
        if fixture: fixture.drop()
