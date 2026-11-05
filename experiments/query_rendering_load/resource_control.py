"""Linux logical-CPU placement controls with thread-level verification."""
from collections import Counter
from contextlib import contextmanager
import os
from pathlib import Path
import time

import psutil

from .arrivals import logical_operation
from .calibrate import quantile
from .isolation_runtime import IsolationTelemetry


def validate_cpus(allowed, target, observer):
    if any(type(c) is not int or c<0 for c in (target,observer)) or target==observer:
        raise ValueError('distinct nonnegative target and observer CPUs required')
    if not {target,observer}.issubset(allowed):
        raise ValueError('chosen CPUs are outside the original affinity mask')


@contextmanager
def observer_affinity(target, observer):
    if not hasattr(os,'sched_getaffinity') or not Path('/proc/self/task').is_dir():
        raise RuntimeError('Linux affinity controls required; no unbound fallback')
    original = set(os.sched_getaffinity(0))
    validate_cpus(original,target,observer)
    # Bind every pre-existing thread; new generator/sampler threads inherit it.
    saved = {}
    try:
        for path in Path('/proc/self/task').iterdir():
            tid = int(path.name)
            try:
                saved[tid] = set(os.sched_getaffinity(tid))
                os.sched_setaffinity(tid,{observer})
            except ProcessLookupError:
                continue
        yield {'original_allowed_cpus':sorted(original),'target':target,'observer':observer}
    finally:
        for tid,cpus in saved.items():
            try:
                os.sched_setaffinity(tid,cpus)
            except ProcessLookupError:
                pass


def affinity_tree(pid, expected, recursive=True):
    records, disappeared = [], []
    root = psutil.Process(pid)
    for process in [root]+(root.children(recursive=True) if recursive else []):
        try:
            identity = {'pid':process.pid,'created_epoch_s':process.create_time(),'name':process.name(),
                        'cgroup':Path(f'/proc/{process.pid}/cgroup').read_text().strip()}
            for path in Path(f'/proc/{process.pid}/task').iterdir():
                tid = int(path.name)
                try:
                    allowed = sorted(os.sched_getaffinity(tid))
                except ProcessLookupError:
                    disappeared.append(tid)
                    continue
                records.append({**identity,'tid':tid,'allowed_cpus':allowed,'expected_cpu':expected,
                                'matches':allowed==[expected]})
        except (FileNotFoundError,ProcessLookupError,psutil.NoSuchProcess):
            disappeared.append(process.pid)
    if not records:
        raise RuntimeError('no live tasks to verify')
    return {'tasks':records,'disappeared_tasks':disappeared,
            'status':'passed' if all(r['matches'] for r in records) else 'failed'}


def classify_operation_result(operation, result):
    if operation['kind']=='write' and result['outcome']=='failed' and not result.get('failure_code'):
        statuses = [step['status'] for step in result.get('steps',[])]
        if statuses and statuses[-1]!=200:
            result.update(failure_code='write_http_failure',upstream_status=statuses[-1])
        elif result.get('error_type') in ('URLError','TimeoutError','ConnectionResetError','RemoteDisconnected'):
            result.update(failure_code='write_transport_failure',upstream_status=None)
        else:
            result.update(failure_code='invalid_write_response',upstream_status=statuses[-1] if statuses else None)
    return result


def resource_operation(operation,*args,**kwargs):
    return classify_operation_result(operation,logical_operation(operation,*args,**kwargs))


def backend_summary(events,prefix):
    selected = [e for e in events if e['operation_id'].startswith(prefix+'-') and
                '-browser-' not in e['operation_id'] and '-warm-' not in e['operation_id']]
    result = {}
    for kind,path in (('read','/articles'),('write','/control/update')):
        rows = [e for e in selected if e['path']==path]
        successful = [e for e in rows if e['status']==200 and e.get('backend') is not None]
        result[kind] = {'traced_requests':len(rows),'http_status_counts':dict(Counter(str(e['status']) for e in rows)),
                        'successful_requests_with_backend_timing':len(successful)}
        for field in ('pool_wait_ms','transaction_wall_ms'):
            values = [e['backend'][field] for e in successful]
            result[kind][field] = {'p50':quantile(values,.5),'p95':quantile(values,.95)}
    result['scope'] = ('Backend timing covers successful traced requests, excludes admission rejections before handlers. '
                       'Pool wait is connection checkout; transaction wall includes DB waits, not isolated query CPU. '
                       'Component quantiles do not sum to end-to-end quantiles.')
    return result


class ResourceTelemetry(IsolationTelemetry):
    def __init__(self, fixture, node_pid, origin, postgres_pid, target, observer, browser_pid=None, browser_cpu=None):
        super().__init__(fixture,node_pid,origin,postgres_pid,browser_pid)
        self.affinity_roots = {'generator_sampler':(os.getpid(),observer),'origin':(origin.pid,target),
                               'next':(node_pid,target),'postgres':(postgres_pid,target)}
        if browser_pid is not None:
            self.affinity_roots['browser'] = (browser_pid,browser_cpu)
        self.affinity_samples = []
        self.affinity_failures = []

    def verify_affinity(self):
        groups = {name:affinity_tree(pid,cpu,recursive=name!='generator_sampler')
                  for name,(pid,cpu) in self.affinity_roots.items()}
        row = {'monotonic_s':time.perf_counter(),'groups':groups}
        self.affinity_samples.append(row)
        self.affinity_failures.extend({'monotonic_s':row['monotonic_s'],'group':name}
            for name,value in groups.items() if value['status']!='passed')

    def sample(self):
        super().sample()
        self.verify_affinity()

    def start(self):
        self.cpu_before = [r._asdict() for r in psutil.cpu_times(percpu=True)]
        super().start()
        if self.affinity_failures:
            self.stop_event.set()
            self.thread.join(timeout=10)
            raise RuntimeError('CPU placement failed before measurement')

    def finish(self):
        self.verify_affinity()
        value = super().finish()
        value.update(affinity_samples=self.affinity_samples,affinity_failures=self.affinity_failures,
                     host_per_cpu_times_before=self.cpu_before,
                     host_per_cpu_times_after=[r._asdict() for r in psutil.cpu_times(percpu=True)])
        value['scope'] += (' CPU placement is verified at start/end and approximately 1 Hz for live threads. '
            'Disjoint logical CPU IDs do not prove distinct physical cores or remove host/hypervisor interference. '
            'Monitoring and the generator share the observer CPU. CPU-time counters span the whole observation including drain.')
        return value
