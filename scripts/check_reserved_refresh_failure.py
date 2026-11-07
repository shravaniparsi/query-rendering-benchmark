"""Verify refresh-failure UI and actual probe handling without performance labels."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from experiments.heldout_tasks.runtime import reserved_runtime
from experiments.heldout_tasks.scale_reference import InventoryScaleReference,AnalyticsScaleReference
from experiments.heldout_tasks.inventory import require


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    output=args.output.resolve();output.mkdir(parents=True,exist_ok=False)
    report={'role':'injected refresh failure correctness','comparative_timing':False,'performance_acquisition':False,
        'source_commit':os.environ.get('GITHUB_SHA'),'status':'started','applications':[]}
    try:
        for task in ('inventory','analytics'):
            folder=output/task;folder.mkdir()
            with reserved_runtime(os.environ['QRL_TEST_DATABASE_URL'],folder,task,'maintained',scale_rows=1000) as runtime:
                config={'task':task,'app_url':runtime['url'],'prefix':'refresh-failure-'+task,'report_path':str(folder/'browser.json')}
                path=folder/'config.json';path.write_text(json.dumps(config,indent=2)+'\n')
                with (folder/'node.log').open('w') as log:subprocess.run([shutil.which('node'),str(ROOT/'experiments/heldout_tasks/refresh_failure_check.cjs'),str(path)],
                    check=True,stdout=log,stderr=subprocess.STDOUT,timeout=150)
                browser=json.loads((folder/'browser.json').read_bytes());require(browser['status']=='passed' and len(browser['probes'])==2,'refresh failure checks incomplete')
                runtime['quiesce']();state=runtime['fixture'].validation_state();reference=(InventoryScaleReference if task=='inventory' else AnalyticsScaleReference)(state)
                reference.verify_final();name='operation_id' if task=='inventory' else 'event_id';operations={row[name]:row for row in state['operations']}
                require(len(operations)==2,'injected reads created unexpected business mutations')
                for row in browser['probes']:
                    reference.verify_query(row['initial']);action=row['action'];identity=action['request'][name]
                    require(operations[identity]['result']=={key:value for key,value in action['result'].items() if key not in ('ok','backend','request_id')}
                        and row['checks_passed'] is True and row['active_control_corruption_rejected'] is True,'actual mutation/failed view differs')
                (folder/'validation-state.json').write_text(json.dumps(state,indent=2)+'\n');report['applications'].append({'task':task,'renderer_checks':2,'real_mutations':2,'injected_refresh_failures':2})
        report['status']='passed'
    finally:
        paths=['apps/reserved-tasks/static/client.js','experiments/heldout_tasks/performance_browser.cjs','experiments/heldout_tasks/refresh_failure_check.cjs','scripts/check_reserved_refresh_failure.py']
        report['source_sha256']={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in paths}
        (output/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report))


if __name__=='__main__':main()
