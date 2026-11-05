"""Fresh reserved SQL/origin/WSGI runtime behind the actual artifact/date gate."""
from contextlib import contextmanager
import hashlib
import ipaddress
import json
import multiprocessing
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
from urllib.request import urlopen
import uuid

from .inventory import require
from .scale_sql import ScaleInventorySQL,ScaleAnalyticsSQL
from .scale_reference import InventoryScaleReference,AnalyticsScaleReference
from .performance_service import PerformanceService,make_server,wal_snapshot
from experiments.query_rendering_load.isolation_runtime import Origin,stop_process
from experiments.study_design.performance_release import PerformancePermission

ROOT=Path(__file__).resolve().parents[2]


def child(pipe,dsn,schema,task,trace_path,cpu,root,release_path,expected_sha,case):
    service=server=thread=None
    try:
        if cpu is not None:os.sched_setaffinity(0,set(cpu))
        permission=PerformancePermission(root,release_path,expected_sha)
        service=PerformanceService(dsn,schema,task,permission,case);server=make_server(service,trace_path)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();pipe.send({'status':'ready','pid':os.getpid(),'port':server.server_port})
        while True:
            name,value=pipe.recv()
            if name=='stop':pipe.send({'status':'ok','result':True});break
            if name=='fault':server.forced_status=value;result=True
            elif name=='wait-idle':result=server.wait_for_idle(35)
            elif name=='quiesce':server.shutdown();result=server.wait_for_idle(35)
            elif name=='diagnostics':result=service.diagnostics()
            elif name=='wal-snapshot':
                require(server.wait_for_idle(35),'native requests did not reach a quiescent counter boundary');result=wal_snapshot(service)
            else:raise ValueError('unknown released origin command')
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


class PerformanceOrigin(Origin):
    def __init__(self,dsn,schema,task,trace_path,cpu,permission,case):
        self.placement='separate_process';self.server=self.service=self.thread=self.process=self.pipe=None
        try:
            context=multiprocessing.get_context('spawn');self.pipe,other=context.Pipe()
            self.process=context.Process(target=child,args=(other,dsn,schema,task,trace_path,cpu,str(permission.root),permission.release_path,permission.expected_sha256,case));self.process.start();other.close()
            value=self._receive(60);require(value['status']=='ready' and value['pid']!=os.getpid(),'released origin isolation/startup failed');self.pid=value['pid'];self.url='http://127.0.0.1:'+str(value['port'])
        except Exception:self.close();raise


def save(path,value):path.write_text(json.dumps(value,indent=2)+'\n')


@contextmanager
def performance_runtime(dsn,output,task,case,permission,*,target_cpu,public_origin):
    require(isinstance(permission,PerformancePermission),'actual release permission required before reserved fixture acquisition')
    permission=PerformancePermission(permission.root,permission.release_path,permission.expected_sha256)
    authorization=permission.permit(task,case['phase'],case['rows'],case['candidate'],case['strategy'],case['collection_date_utc'],context_id=case.get('context_id'))
    relative='experiments/heldout_tasks/performance_runtime.py'
    require(hashlib.sha256(Path(__file__).read_bytes()).hexdigest()==permission._source_sha[relative],'executing performance runtime differs from frozen source')
    endpoint=urlsplit(public_origin);address=ipaddress.ip_address(endpoint.hostname)
    require(endpoint.scheme=='http' and endpoint.port==8765 and endpoint.path==endpoint.query==endpoint.fragment=='' and endpoint.username is None and
        address.version==4 and address in ipaddress.ip_network('100.64.0.0/10'),'fixed private research gateway origin required')
    output=Path(output);output.mkdir(parents=True,exist_ok=True);fixture=origin=process=None;drained=False;start=time.perf_counter_ns()
    try:
        cls=ScaleInventorySQL if task=='inventory' else ScaleAnalyticsSQL;fixture=cls(dsn,'qrl_reserved_'+uuid.uuid4().hex);fixture.initialize_scale(case['rows'],case['candidate'])
        tables=['metadata','operations','products','reservations'] if task=='inventory' else ['metadata','operations','events']
        if case['candidate']=='maintained':tables.append('stock_balances' if task=='inventory' else 'event_buckets')
        with fixture.connection() as connection:
            for table in tables:connection.execute(f'VACUUM ANALYZE {table}')
            connection.execute('CHECKPOINT')
            storage=[dict(zip(('relation','bytes'),row)) for row in connection.execute("SELECT relname,pg_total_relation_size(oid) FROM pg_class WHERE relnamespace=%s::regnamespace AND relkind='r' ORDER BY relname",(fixture.schema,))]
            version=connection.execute('SELECT version()').fetchone()[0]
        state=fixture.validation_state();reference=(InventoryScaleReference if task=='inventory' else AnalyticsScaleReference)(state);initial=reference.verify_final();save(output/'initial-state.json',state)
        origin=PerformanceOrigin(dsn,fixture.schema,task,output/'backend.jsonl',target_cpu,permission,case)
        with socket.socket() as probe:probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
        url='http://127.0.0.1:'+str(port);prefix=['taskset','-c',','.join(map(str,target_cpu))]
        with (output/'web.log').open('w') as log:
            process=subprocess.Popen(prefix+[sys.executable,'-m','gunicorn','--bind',f'127.0.0.1:{port}','--worker-class','gthread','--workers','1','--threads','8','--timeout','15','--graceful-timeout','5',
                '--access-logfile','-','--error-logfile','-','web:app'],cwd=ROOT/'apps/reserved-tasks',env=dict(os.environ,RESERVED_ORIGIN=origin.url,RESERVED_TASK=task,RESERVED_PUBLIC_ORIGIN=public_origin),stdout=log,stderr=subprocess.STDOUT)
            deadline=time.monotonic()+30
            while True:
                try:
                    with urlopen(url+'/health',timeout=1):break
                except OSError:
                    if process.poll() is not None or time.monotonic()>deadline:raise RuntimeError('released WSGI startup failed')
                    time.sleep(.1)
            finish=time.perf_counter_ns();save(output/'preparation.json',{'authorization':authorization,'setup_start_monotonic_ns':str(start),'setup_finish_monotonic_ns':str(finish),'setup_wall_s':(finish-start)/1e9,
                'initial_oracle':initial,'postgres':version,'initial_relation_storage':storage,'preparation':'Fresh deterministic seed, vacuum/analyze, checkpoint and native initial-state reconstruction; later HTTP warmups are separate.'})
            def quiesce():
                nonlocal drained
                if not drained:
                    stop_process(process);require(origin.command('quiesce'),'released origin failed to drain');drained=True
                return True
            yield {'fixture':fixture,'origin':origin,'process':process,'url':url,'quiesce':quiesce,'task':task,'query_kind':case['candidate'],'purpose':'heldout_performance',
                'comparative_timing':True,'authorization':authorization,'setup_wall_s':(finish-start)/1e9}
    finally:
        stop_process(process)
        if origin:origin.close()
        if fixture:fixture.drop()
