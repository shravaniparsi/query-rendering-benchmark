"""Exercise the actual contained non-root Node/Chromium path without app traffic."""
import argparse
import json
import os
from pathlib import Path
import platform
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from experiments.study_design.decision import require
from experiments.issue_dashboard.loaded_browser import LoadedBrowser,check_clock


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    require(platform.system()=='Linux' and os.environ.get('GITHUB_ACTIONS')=='true' and os.environ.get('RUNNER_OS')=='Linux' and os.getuid()>0,'disposable non-root Linux Actions runtime check required')
    event=json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_bytes());require(event['repository']['private'] is False and os.environ['GITHUB_REPOSITORY']=='shravaniparsi/query-rendering-benchmark','free public benchmark runner required')
    args.output.mkdir(parents=True,exist_ok=False);cpus=sorted(os.sched_getaffinity(0));require(len(cpus)>=4,'full standard host inventory required')
    check_clock(args.output/'clock.json');spec={'duration_s':0,'loaded_browser_offsets_s':[],'loaded_browser_deadline_ms':4000,'loaded_browser_max_dispatch_lag_s':.1}
    browser=LoadedBrowser('http://127.0.0.1',args.output,{'name':'runtime-only','strategy':'ssr'},cpus[1],spec,script=ROOT/'experiments/heldout_tasks/browser_runtime_check.cjs')
    result_path=args.output/'runtime-browser-result.json';browser.config.update(runtime_check_only=True,runtime_result_path=str(result_path))
    try:
        browser.launch();sample=browser.sample();(args.output/'browser-budget.json').write_text(json.dumps(sample,indent=2)+'\n')
    finally:browser.close()
    result=json.loads(result_path.read_bytes());require(browser.process.returncode==0 and result['status']=='completed' and result['uid']==os.getuid()
        and result['gid']==os.getgid() and result['application_requests_sent']==0 and result['local_content_verified'] is True
        and result['comparative_performance_acquired'] is False and result['cgroup']=='0::/qrl_browser_observer','browser runtime/containment/cleanup check failed')
    report={'status':'passed','role':'actual browser startup and containment only','source_commit':os.environ['GITHUB_SHA'],'run_id':os.environ['GITHUB_RUN_ID'],
        'full_host_cpus':cpus,'browser_cpu':cpus[1],'uid':result['uid'],'browser':result['browser'],'application_requests_sent':0,'comparative_performance_acquired':False}
    (args.output/'report.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report))


if __name__=='__main__':main()
