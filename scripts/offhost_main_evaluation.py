"""One claimed public-runner UTC-date attempt of the frozen reserved study."""
import argparse
from datetime import datetime,timezone
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from experiments.heldout_tasks.inventory import require
from experiments.study_design.performance_release import PerformancePermission


def execution_environment(permission,day,smoke):
    require(platform.system()=='Linux','released study requires Linux process/cgroup clocks')
    require(os.environ.get('GITHUB_RUN_ATTEMPT')=='1','job re-runs cannot replace a calendar attempt')
    repo=os.environ['GITHUB_REPOSITORY'];event=json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_bytes())
    require(event['repository']['private'] is False and event['repository']['full_name']==repo and os.environ.get('QRL_REPOSITORY_VISIBILITY')=='public','acquisition disabled in private repositories under the zero-spending plan')
    require(repo==permission.artifacts['sample_plan']['execution_repository'],'unexpected execution repository')
    require(os.environ.get('QRL_DATE_CLAIM_RUN_ID')==os.environ['GITHUB_RUN_ID'],'atomic date-claim identity missing')
    require(os.environ['GITHUB_SHA']==os.environ['QRL_FROZEN_EXECUTION_SHA'],'executing checkout differs from separately frozen execution commit')
    clean=subprocess.run(['git','status','--porcelain','--untracked-files=no'],cwd=ROOT,check=True,capture_output=True,text=True).stdout
    require(not clean,'tracked source/artifact modifications before acquisition')
    sample=permission.artifacts['sample_plan'];require(smoke==(day in sample.get('qualification_dates_utc',[])),'qualification and main date modes cannot be interchanged')
    require(day in sample['collection_dates_utc'] or day in sample.get('qualification_dates_utc',[]),'unplanned real UTC date')
    require(datetime.now(timezone.utc).date().isoformat()==day,'actual date differs from claimed UTC date')
    require(len(os.sched_getaffinity(0))>=4,'public standard host must expose at least four CPUs before selecting two')
    return {'status':'passed','additional_spending_limit':0,'repository':repo,'visibility':'public','github_run_id':os.environ['GITHUB_RUN_ID'],'run_attempt':'1',
        'collection_date_utc':day,'execution_source_commit':os.environ['GITHUB_SHA'],'release_sha256':permission.expected_sha256,'full_host_cpus':sorted(os.sched_getaffinity(0))}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--role',choices=('target','observer'),required=True);parser.add_argument('--task',choices=('inventory','analytics'),required=True)
    parser.add_argument('--day',required=True);parser.add_argument('--release',required=True);parser.add_argument('--release-sha256',required=True);parser.add_argument('--smoke',action='store_true');args=parser.parse_args()
    permission=PerformancePermission(ROOT,args.release,args.release_sha256);authorization=execution_environment(permission,args.day,args.smoke)
    # All acquisition imports follow actual artifact/source/public-date verification.
    from experiments.heldout_tasks.main_design import cells
    from experiments.heldout_tasks.performance_target import PerformanceTarget
    from experiments.heldout_tasks.performance_observer import run_observer
    from experiments.heldout_tasks.gateway import gateway
    from experiments.issue_dashboard.loaded_browser import check_clock
    from experiments.issue_dashboard.offhost_engineering import save
    from scripts.offhost_connectivity import manifest,node_name,network_state,private_ip
    output=ROOT/'ci-artifacts'/('main-'+args.task);output.mkdir(parents=True,exist_ok=False)
    # A numeric pair namespace distinguishes two applications within one Actions run.
    pair_id=os.environ['GITHUB_RUN_ID']+('0' if args.task=='inventory' else '1')
    own=manifest(args.role,pair_id,'1',os.environ['GITHUB_SHA']);own['github_run_id']=os.environ['GITHUB_RUN_ID'];own['task']=args.task
    peer_name=node_name(pair_id,'1','observer' if args.role=='target' else 'target')
    report={'role':'frozen reserved daily evaluation','task':args.task,'collection_date_utc':args.day,'smoke':args.smoke,'confirmatory_data':not args.smoke,
        'authorization':authorization,'local':own,'remote':None,'cases':cells(permission.artifacts,args.task,args.day,args.smoke),'cells':[],'status':'started',
        'started_at_utc':datetime.now(timezone.utc).isoformat(),'platform':platform.platform(),'python':platform.python_version(),'runner_image':os.environ.get('ImageVersion'),
        'packages':{n:importlib.metadata.version(n) for n in ('Flask','gunicorn','psycopg','psutil')},'source_sha256':permission._source_sha}
    save(output/'report.json',report);target=server=None
    try:
        state=network_state(own['overlay_name'],peer_name,timeout=300);report['network_discovery']=state
        if args.role=='observer':
            check_clock(output/'browser-clock.json');report['remote']=run_observer(output,own,peer_name,private_ip(state['peer']['TailscaleIPs']),args.task,args.day,args.smoke,permission,report['cells'])
        else:
            public_origin='http://'+private_ip(state['self']['TailscaleIPs'])+':8765'
            target=PerformanceTarget(os.environ['QRL_TEST_DATABASE_URL'],output,own,peer_name,args.task,args.day,args.smoke,permission,public_origin)
            server=gateway(target,private_ip(state['self']['TailscaleIPs']),private_ip(state['peer']['TailscaleIPs']));deadline=time.monotonic()+4800
            while not target.finished and time.monotonic()<deadline:
                server.handle_request()
                if time.monotonic()-target.last>600:raise RuntimeError('observer idle timeout')
            require(target.finished and not target.errors,'daily target protocol incomplete/errors');report.update(remote=target.remote,cells=target.cells)
        require(datetime.now(timezone.utc).date().isoformat()==args.day,'attempt crossed fixed UTC date');report['status']='passed'
    except Exception as error:
        report.update(status='failed',error_type=type(error).__name__,error=str(error)[:500]);raise
    finally:
        try:
            if server:server.server_close();report['connection_rejections']=server.connection_rejections
            if target:
                report.update(gateway_errors=target.errors,proxy_transport_failures=target.proxy_transport_failures,cells=target.cells,remote=target.remote)
                target.close();save(output/'connection-observations.json',target.connection_observations);save(output/'identity-observations.json',target.identity_observations)
        except Exception as error:report.update(status='failed',cleanup_error_type=type(error).__name__);raise
        finally:report['finished_at_utc']=datetime.now(timezone.utc).isoformat();save(output/'report.json',report)


if __name__=='__main__':main()
