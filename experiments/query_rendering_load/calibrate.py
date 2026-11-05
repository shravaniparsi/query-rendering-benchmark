"""Engineering rate calibration on the competent baseline, never comparative data."""
import argparse
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import random
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from urllib.request import urlopen

from .arrivals import execute, logical_operation, trace
from .fixture import Fixture
from .service import FeedService, make_server

ROOT = Path(__file__).resolve().parents[2]


def quantile(values, fraction):
    if not values:
        return None
    return sorted(values)[max(0, math.ceil(len(values)*fraction)-1)]


def summarize(records, duration):
    outcomes = Counter(row['outcome'] for row in records)
    completed = [row for row in records if row['outcome'] == 'completed']
    # Include drain time in throughput; deadline-censored operations remain explicit.
    elapsed = max([duration] + [row.get('complete_s', row.get('observed_s', 0)) for row in records])
    latencies = [(row['complete_s']-row['intended_s'])*1000 for row in completed]
    lag = [(row['dispatch_s']-row['intended_s'])*1000 for row in records if 'dispatch_s' in row]
    return {'offered': len(records), 'outcomes': dict(outcomes),
            'completed_throughput_rps_including_drain': len(completed)/elapsed,
            'observed_window_s': elapsed, 'completed_only_p50_ms': quantile(latencies, .5),
            'completed_only_p95_ms': quantile(latencies, .95),
            'dispatch_lag_p95_ms': quantile(lag, .95),
            'generator_limited': bool(outcomes['admission_drop'] or outcomes['dispatch_lag_drop']),
            'censored': outcomes['drain_timeout']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rates', default='4,16,64,128,256')
    parser.add_argument('--duration', type=float, default=8)
    parser.add_argument('--rows', type=int, default=100000)
    args = parser.parse_args()
    rates = [float(value) for value in args.rates.split(',')]
    if not rates or rates != sorted(set(rates)) or any(not math.isfinite(r) or r <= 0 or r > 512 for r in rates):
        parser.error('rates must be unique ascending positive values at most 512')
    if not 1 <= args.duration <= 30 or not 1000 <= args.rows <= 100000:
        parser.error('engineering duration/size bounds exceeded')
    output = Path(os.environ.get('QRL_CALIBRATION_OUTPUT', ROOT/'data/calibration'/('rate-'+time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()))))
    output.mkdir(parents=True, exist_ok=False)
    report = {'role':'engineering calibration only', 'confirmatory_data':False,
              'configuration':{'rates_rps':rates, 'duration_s':args.duration, 'rows':args.rows,
                               'rendering':'ssr', 'query':'competent baseline without treatment index',
                               'pool':4, 'http_handlers':16, 'generator_workers':64,
                               'max_dispatch_lag_s':.05, 'drain_timeout_s':15, 'write_fraction':0},
              'environment':{'platform':platform.platform(), 'commit':os.environ.get('GITHUB_SHA'),
                             'run_id':os.environ.get('GITHUB_RUN_ID'), 'runner_image':os.environ.get('ImageVersion')},
              'trials':[]}
    sources = list(Path(__file__).parent.glob('*.py')) + [ROOT/'.github/workflows/query-rendering-engineering.yml']
    report['source_sha256'] = {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
    with tempfile.TemporaryDirectory(prefix='qrl-calibration-') as directory:
        pg = None
        dsn = os.environ.get('QRL_TEST_DATABASE_URL')
        if not dsn:
            import pgserver
            pg = pgserver.get_server(Path(directory)/'pgdata', cleanup_mode='stop')
            dsn = pg.get_uri()
        fixture = service = server = process = None
        try:
            fixture = Fixture(dsn, 'qrl_cal_' + uuid.uuid4().hex)
            fixture.initialize(args.rows, indexed=False)
            report['database'] = fixture.diagnostics()
            if os.environ.get('QRL_EXPECT_POSTGRES_MAJOR') and not report['database']['version'].startswith(
                    'PostgreSQL '+os.environ['QRL_EXPECT_POSTGRES_MAJOR']+'.'):
                raise RuntimeError('unexpected database version')
            service = FeedService(dsn, fixture.schema, size=4)
            server = make_server(0, service, max_handlers=16, trace_path=output/'backend.jsonl')
            thread = threading.Thread(target=server.serve_forever, daemon=True)
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
                        with urlopen(url, timeout=1):
                            break
                    except OSError:
                        if process.poll() is not None or time.monotonic()>deadline:
                            raise RuntimeError('app startup failed')
                        time.sleep(.2)
                warm = execute(trace(4,3),lambda item: logical_operation(item,'ssr',url,origin,'warm'),workers=4)
                (output/'warmup.json').write_text(json.dumps(warm,indent=2)+'\n')
                if any(row['outcome']!='completed' for row in warm):
                    raise RuntimeError('warmup failed')
                settings = [(f'rate-{i}',rate,args.duration,True) for i,rate in enumerate(rates)]
                diagnostics = [('trace-on',16,6,True),('trace-off',16,6,False)]
                random.Random(20261029).shuffle(diagnostics)
                settings += diagnostics
                for name,rate,duration,tracing in settings:
                    server.phase = name
                    server.trace_enabled = tracing
                    records = execute(trace(rate,duration),lambda item: logical_operation(item,'ssr',url,origin,name,timeout=5),
                                      workers=64,drain_timeout=15)
                    (output/(name+'.json')).write_text(json.dumps(records,indent=2)+'\n')
                    summary = summarize(records,duration)
                    with server.admission_lock:
                        summary['origin_handler_admission_rejections'] = server.admission_rejections[name]
                    report['trials'].append({'name':name,'offered_rate_rps':rate,'duration_s':duration,
                                             'tracing':tracing,**summary})
                    if summary['censored']:
                        raise RuntimeError('drain deadline reached; no further cells permitted')
                # Bracket only; there is no latency SLO, independent replication,
                # write workload or validated browser endpoint in this calibration.
                report['status']='passed'
                report['interpretation']='Engineering rate ladder only; generator-limited cells cannot establish target capacity. Trace on/off trials are single diagnostic windows, not an overhead correction.'
        except Exception as error:
            report['status']='failed'
            report['error_type']=type(error).__name__
            raise
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
            if pg:
                pg.cleanup()
            (output/'report.json').write_text(json.dumps(report,indent=2)+'\n')
            print('Calibration report:',output/'report.json')


if __name__=='__main__':
    main()
