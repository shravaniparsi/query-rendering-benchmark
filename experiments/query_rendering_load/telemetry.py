"""Explicitly scoped engineering counters; no per-query CPU attribution."""
import os
import threading
import time

import psutil


def lsn_bytes(value):
    high, low = value.split('/')
    return (int(high,16)<<32)+int(low,16)


def process_snapshot(pid, recursive=False):
    if pid is None:
        return {'processes':{}, 'errors':['root_pid_unavailable']}
    records, errors = {}, []
    try:
        root = psutil.Process(pid)
        processes = [root]+(root.children(recursive=True) if recursive else [])
    except (psutil.Error, ValueError) as error:
        return {'processes':{}, 'errors':[type(error).__name__]}
    for process in processes:
        try:
            with process.oneshot():
                identity = f'{process.pid}:{process.create_time()}'
                cpu = process.cpu_times()
                record = {'cpu_s':cpu.user+cpu.system, 'rss_bytes':process.memory_info().rss}
                try:
                    io = process.io_counters()
                    record['io_read_bytes'], record['io_write_bytes'] = io.read_bytes, io.write_bytes
                except (psutil.Error, AttributeError, NotImplementedError):
                    record['io_read_bytes'] = record['io_write_bytes'] = None
                records[identity] = record
        except psutil.Error as error:
            errors.append(type(error).__name__)
    return {'processes':records, 'errors':errors}


def process_delta(before, after):
    common = set(before['processes']) & set(after['processes'])
    result = {'matched_processes':len(common),
              'partial_process_coverage': bool(before['errors'] or after['errors'] or
                                               set(before['processes'])!=set(after['processes'])),
              'rss_sum_before_bytes':sum(v['rss_bytes'] for v in before['processes'].values()) or None,
              'rss_sum_after_bytes':sum(v['rss_bytes'] for v in after['processes'].values()) or None}
    for field in ('cpu_s','io_read_bytes','io_write_bytes'):
        valid = [key for key in common if before['processes'][key][field] is not None and after['processes'][key][field] is not None]
        changes = [after['processes'][key][field]-before['processes'][key][field] for key in valid]
        result[field+'_delta_observed'] = sum(changes) if changes and min(changes)>=0 else None
        result[field+'_covered_processes'] = len(valid)
    return result


class Telemetry:
    def __init__(self, fixture, node_pid, postgres_pid=None, interval=1):
        self.fixture, self.interval = fixture, interval
        self.groups = {'python_service_generator_sampler':(os.getpid(),False),
                       'next_tree':(node_pid,True), 'postgres_tree':(postgres_pid,True)}
        self.samples, self.errors = [], []
        self.stop_event = threading.Event()

    def db_snapshot(self):
        with self.fixture.driver.connect(self.fixture.dsn, autocommit=True,
                                         application_name='qrl_telemetry') as conn:
            conn.execute('SELECT pg_stat_clear_snapshot()')
            lsn = conn.execute('SELECT pg_current_wal_insert_lsn()::text').fetchone()[0]
            stats = conn.execute('''SELECT xact_commit,xact_rollback,blks_read,blks_hit,
                tup_inserted,tup_updated,deadlocks,temp_bytes,stats_reset::text
                FROM pg_stat_database WHERE datname=current_database()''').fetchone()
            sizes = conn.execute('''SELECT pg_total_relation_size(%s::regclass),
                pg_indexes_size(%s::regclass),pg_total_relation_size(%s::regclass)''',
                (self.fixture.schema+'.articles',self.fixture.schema+'.articles',self.fixture.schema+'.operations')).fetchone()
        return {'wal_insert_lsn':lsn,
                'database_stats':dict(zip(('xact_commit','xact_rollback','blks_read','blks_hit','tup_inserted','tup_updated','deadlocks','temp_bytes','stats_reset'),stats)),
                'article_total_relation_bytes':sizes[0], 'article_index_bytes':sizes[1],
                'operation_ledger_total_bytes':sizes[2]}

    def sample(self):
        start = time.perf_counter()
        with self.fixture.driver.connect(self.fixture.dsn,autocommit=True,
                                         application_name='qrl_telemetry') as conn:
            waits = conn.execute('''SELECT state,wait_event_type,wait_event,count(*)
                FROM pg_stat_activity WHERE application_name=%s GROUP BY 1,2,3''',
                ('qrl:'+self.fixture.schema,)).fetchall()
            locks = conn.execute('''SELECT count(*) FROM pg_locks WHERE NOT granted
                AND pid IN (SELECT pid FROM pg_stat_activity WHERE application_name=%s)''',
                ('qrl:'+self.fixture.schema,)).fetchone()[0]
        self.samples.append({'monotonic_s':start,'sampling_wall_ms':(time.perf_counter()-start)*1000,
                             'pool_backend_states':[list(row) for row in waits], 'ungranted_pool_locks':locks})

    def start(self):
        self.db_before = self.db_snapshot()
        self.process_before = {key:process_snapshot(*value) for key,value in self.groups.items()}
        self.sample()
        def run():
            while not self.stop_event.wait(self.interval):
                try:
                    self.sample()
                except Exception as error:
                    self.errors.append(type(error).__name__)
        self.thread = threading.Thread(target=run,daemon=True)
        self.thread.start()

    def finish(self):
        self.stop_event.set()
        self.thread.join(timeout=10)
        if self.thread.is_alive():
            raise RuntimeError('telemetry sampling did not stop')
        process_after = {key:process_snapshot(*value) for key,value in self.groups.items()}
        db_after = self.db_snapshot()
        stats_before, stats_after = self.db_before['database_stats'],db_after['database_stats']
        stats_reset = stats_before['stats_reset']!=stats_after['stats_reset']
        stats_delta = {key:None if stats_reset or stats_after[key]<value else stats_after[key]-value
                       for key,value in stats_before.items() if key!='stats_reset'}
        return {'db_before':self.db_before, 'db_after':db_after,
                'cluster_wal_insert_bytes_delta':lsn_bytes(db_after['wal_insert_lsn'])-lsn_bytes(self.db_before['wal_insert_lsn']),
                'database_stats_delta':stats_delta,
                'process_before':self.process_before,'process_after':process_after,
                'process_deltas':{key:process_delta(self.process_before[key],process_after[key]) for key in self.groups},
                'samples':self.samples, 'sampling_errors':self.errors,
                'scope':'WAL is cluster-wide including ledger/background work; cumulative database stats can lag and include monitoring. Python CPU includes service, generator and sampler. Process CPU is observed matched-process CPU, not query CPU. RSS sums can double-count shared pages; I/O may be unavailable. Sparse lock samples cannot prove absence of waits.'}
