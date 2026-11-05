"""PostgreSQL 18 pool-backend WAL/I/O snapshots at quiescent boundaries."""
from contextlib import ExitStack
import math

WAL_FIELDS=('wal_records','wal_fpi','wal_bytes','wal_buffers_full')
IO_FIELDS=('reads','read_bytes','read_time','writes','write_bytes','write_time',
           'extends','extend_bytes','extend_time','fsyncs','fsync_time')


def snapshot(service):
    # Reserve all pool connections: no SQL can overlap these boundary probes.
    with ExitStack() as stack:
        connections=[stack.enter_context(service.pool.acquire())[0] for _ in range(service.pool.available.maxsize)]
        pids=sorted(c.info.backend_pid for c in connections)
        settings=[]
        for conn in connections:
            conn.execute('SELECT pg_stat_force_next_flush()').fetchone()
            settings.append({'pid':conn.info.backend_pid,'track_wal_io_timing':
                conn.execute("SELECT current_setting('track_wal_io_timing')").fetchone()[0]})
        with service.fixture.driver.connect(service.fixture.dsn,autocommit=True,application_name='qrl_wal_observer') as conn:
            if int(conn.execute("SELECT current_setting('server_version_num')").fetchone()[0])<180000:
                raise RuntimeError('PostgreSQL 18 per-backend statistics required')
            conn.execute("SET stats_fetch_consistency TO 'none'")
            backends=[]
            for pid in pids:
                identity=conn.execute('SELECT pid,backend_start::text,application_name FROM pg_stat_activity WHERE pid=%s',(pid,)).fetchone()
                if identity is None or identity[2]!='issue:'+service.fixture.schema:raise ValueError('pool backend identity differs')
                wal=conn.execute('SELECT to_jsonb(s) FROM pg_stat_get_backend_wal(%s) s',(pid,)).fetchone()[0]
                io=[r[0] for r in conn.execute("SELECT to_jsonb(s) FROM pg_stat_get_backend_io(%s) s WHERE object='wal' ORDER BY context",(pid,))]
                backends.append({'pid':pid,'backend_start':identity[1],'application_name':identity[2],'wal':wal,'io':io})
            cluster=conn.execute('SELECT to_jsonb(s) FROM pg_stat_wal s').fetchone()[0]
            cluster_io=[r[0] for r in conn.execute("SELECT to_jsonb(s) FROM pg_stat_io s WHERE object='wal' ORDER BY backend_type,context")]
        return {'format':'qrl-pool-wal-v1','settings':sorted(settings,key=lambda r:r['pid']),
                'backends':backends,'cluster_wal':cluster,'cluster_io':cluster_io,
                'scope':'Pool-owned WAL records and I/O invoked by those backends; group commit/background activity prevents per-request flush attribution.'}


def counter_delta(before,after,fields):
    if before.get('stats_reset')!=after.get('stats_reset'):raise ValueError('statistics reset changed')
    result={}
    for key in fields:
        a,b=before[key],after[key]
        if (a is None)!=(b is None):raise ValueError('counter availability changed')
        if a is None:result[key]=None;continue
        if type(a) not in (int,float) or type(b) not in (int,float) or not math.isfinite(a) or not math.isfinite(b) or b<a:
            raise ValueError('invalid/nonmonotone statistics counter')
        result[key]=b-a
    return result


def delta(before,after,instrumented):
    if before['format']!='qrl-pool-wal-v1' or after['format']!=before['format']:raise ValueError('wrong WAL snapshot format')
    left={r['pid']:r for r in before['backends']};right={r['pid']:r for r in after['backends']}
    if len(left)!=4 or len(before['backends'])!=4 or len(after['backends'])!=4 or set(left)!=set(right):raise ValueError('four stable pool PIDs required')
    setting='on' if instrumented else 'off'
    for value in (before,after):
        if len(value['settings'])!=4 or {r['pid'] for r in value['settings']}!=set(left) or any(r['track_wal_io_timing']!=setting for r in value['settings']):
            raise ValueError('pool WAL timer settings differ')
    rows=[]
    for pid,a in left.items():
        b=right[pid]
        if any(a[k]!=b[k] for k in ('backend_start','application_name')):raise ValueError('pool PID was replaced/reused')
        wal=counter_delta(a['wal'],b['wal'],WAL_FIELDS)
        ai={r['context']:r for r in a['io']};bi={r['context']:r for r in b['io']}
        if not ai or len(ai)!=len(a['io']) or len(bi)!=len(b['io']) or set(ai)!=set(bi):raise ValueError('WAL I/O contexts unavailable/changed')
        io=[]
        for context in ai:
            for value in (ai[context],bi[context]):
                if value['object']!='wal' or value['backend_type']!='client backend':raise ValueError('unexpected WAL I/O ownership')
            io.append({'context':context,**counter_delta(ai[context],bi[context],IO_FIELDS)})
        rows.append({'pid':pid,'wal':wal,'io':io})
    totals={key:sum(r['wal'][key] for r in rows) for key in WAL_FIELDS}
    io_totals={}
    for key in IO_FIELDS:
        values=[s[key] for r in rows for s in r['io'] if s[key] is not None]
        io_totals[key]=sum(values) if values else None
    if not instrumented and any(io_totals[key] not in (0,None) for key in ('read_time','write_time','extend_time','fsync_time')):
        raise ValueError('disabled WAL timers accumulated time')
    return {'backends':rows,'pool_wal':totals,'pool_io':io_totals,
            'scope':'Backend-counter window, not a request decomposition or isolated counter-maintenance cost.'}
