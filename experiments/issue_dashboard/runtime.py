"""Disposable independent issue origin and production WSGI application."""
from contextlib import contextmanager
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid
from urllib.request import urlopen

from experiments.query_rendering_load.isolation_runtime import stop_process
from .fixture import IssueFixture
from .service import IssueOrigin
from .cpu_budget import mask

ROOT = Path(__file__).resolve().parents[2]


@contextmanager
def issue_runtime(dsn,output,kind,rows=1000,fanout=20,*,target_cpu=None,pool_size=4,prepare_threshold=5,checkpoint=False,
                  write_phase_tracing=False,durable_writes=False,wal_io_timing=None,read_operation='recent',
                  distribution_family=None,distribution_seed=None,ranking_design=None,ranking_resource_timing=False):
    if type(ranking_resource_timing) is not bool:raise ValueError('boolean ranking resource timing required')
    if read_operation not in ('recent','discussion_rank'):raise ValueError('unknown read operation')
    fixture_class=IssueFixture;service_factory=None;web_module='web:app'
    if read_operation=='discussion_rank':
        from .ranking import RankingFixture,RankingService
        fixture_class=RankingFixture;service_factory=RankingService;web_module='ranked_web:app'
    initialize_options={}
    if distribution_family is not None:
        from .distribution import DistributionFixture,DistributionRecentService,DistributionRankingService
        fixture_class=DistributionFixture
        service_factory=DistributionRankingService if read_operation=='discussion_rank' else DistributionRecentService
        initialize_options={'distribution_family':distribution_family,'distribution_seed':distribution_seed,'read_operation':read_operation}
    elif distribution_seed is not None:
        raise ValueError('distribution seed requires an explicit family')
    if ranking_design is not None:
        from .ranking_design import RankingDesignFixture,RankingDesignService,design_metadata
        if kind!='maintained' or read_operation!='discussion_rank' or distribution_family is None:
            raise ValueError('ranking design requires maintained discussion ranking and an explicit distribution')
        design_metadata(ranking_design)
        fixture_class=RankingDesignFixture;service_factory=RankingDesignService
        initialize_options['ranking_design']=ranking_design
    if ranking_resource_timing:
        if read_operation!='discussion_rank' or distribution_family is None:
            raise ValueError('ranking resource timing requires versioned distribution ranking')
        from .endtoend_service import DistributionResourceService,DesignResourceService
        service_factory=DesignResourceService if ranking_design is not None else DistributionResourceService
    fixture=origin=process=None
    try:
        fixture=fixture_class(dsn,'qrl_issue_'+uuid.uuid4().hex)
        fixture.initialize(rows,fanout,kind,**initialize_options);fixture.prepare_maintenance()
        if checkpoint:fixture.checkpoint()
        origin=IssueOrigin(dsn,fixture.schema,output/'backend.jsonl',cpu=target_cpu,
                           pool_size=pool_size,prepare_threshold=prepare_threshold,
                           write_phase_tracing=write_phase_tracing,durable_writes=durable_writes,wal_io_timing=wal_io_timing,service_factory=service_factory)
        with socket.socket() as socket_probe:
            socket_probe.bind(('127.0.0.1',0));port=socket_probe.getsockname()[1]
        url=f'http://127.0.0.1:{port}'
        with (output/'web.log').open('w') as log:
            command_prefix=['taskset','-c',','.join(map(str,mask(target_cpu)))] if target_cpu is not None else []
            process=subprocess.Popen(command_prefix+[sys.executable,'-m','gunicorn','--bind',f'127.0.0.1:{port}',
                '--worker-class','gthread','--workers','1','--threads','8','--timeout','15',
                '--graceful-timeout','5','--access-logfile','-','--error-logfile','-',web_module],
                cwd=ROOT/'apps/issue-dashboard',env=dict(os.environ,ISSUE_ORIGIN=origin.url),
                stdout=log,stderr=subprocess.STDOUT)
            deadline=time.monotonic()+30
            while True:
                try:
                    with urlopen(url+'/health',timeout=1):break
                except OSError:
                    if process.poll() is not None or time.monotonic()>deadline:raise RuntimeError('issue WSGI startup failed')
                    time.sleep(.1)
            def quiesce():
                stop_process(process);origin.command('quiesce')
            yield {'fixture':fixture,'origin':origin,'process':process,'url':url,'quiesce':quiesce}
    finally:
        stop_process(process)
        if origin:origin.close()
        if fixture:fixture.drop()
