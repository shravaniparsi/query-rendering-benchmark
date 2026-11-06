"""Retain a claimed day's audit coverage and exact descriptive paired scores."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from experiments.study_design.decision import require
from experiments.study_design.performance_release import PerformancePermission
from experiments.study_design.public_github import GitHub
from experiments.study_design.public_artifacts import assets,download,extract
from experiments.study_design.main_analysis import day_summary,campaign
from scripts.inspect_public_attempt import inspect


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--run-id',required=True);args=parser.parse_args()
    permission=PerformancePermission(ROOT,'release/release.json',os.environ['QRL_RELEASE_SHA256']);api=GitHub(os.environ['GITHUB_REPOSITORY'],os.environ['GH_TOKEN'])
    attempt=inspect(api,permission,args.run_id,os.environ['QRL_FROZEN_EXECUTION_SHA']);claim=attempt['claim'];require(claim is not None,'actual claimed acquisition required')
    release=api.ensure_release('replay-'+args.run_id,attempt['source_commit'],permission.expected_sha256);inventory=assets(api,release);components=[];reports=[]
    for task in ('inventory','analytics'):
        result={'task':task,'status':'unavailable'}
        try:
            with tempfile.TemporaryDirectory(prefix='qrl-daily-summary-') as temp:
                root=Path(temp);stem=f'application-replay-{args.run_id}-{task}'
                for suffix in ('.json','.tar.gz'):download(api,release,inventory[stem+suffix],root/(stem+suffix))
                extract(root/(stem+'.tar.gz'),root/(stem+'.json'),root/'extracted');status=json.loads((root/'extracted/application-replay.json').read_bytes())
                require(status['task']==task and status['run_id']==args.run_id and status['source_commit']==attempt['source_commit'] and status['release_sha256']==permission.expected_sha256
                    and all(status[k]==v for k,v in claim.items()) and status['analysis_workflow_run_id']==os.environ['GITHUB_RUN_ID'],'application replay provenance differs')
                require(status['status'] in ('passed','unavailable'),'unknown application replay outcome');result.update(status=status['status'],replay=status)
                if status['status']=='passed' and claim['mode']=='main':
                    value=json.loads((root/'extracted/component.json').read_bytes());campaign(permission.artifacts,[value],attempt['source_commit'],permission.expected_sha256);components.append(value)
        except Exception as error:result.update(status='unavailable',error_type=type(error).__name__,error=str(error)[:500])
        reports.append(result)
    passed=all(r['status']=='passed' for r in reports);hosts=[role for row in reports if row['status']=='passed' for role in row['replay']['roles']]
    distinct=len(hosts)==4 and len({h['node_id'] for h in hosts})==4 and len({h['kernel_boot_sha256'] for h in hosts})==4
    if passed and not distinct:components=[]
    summary={'version':'public-attempt-summary-v1','parent_run_id':args.run_id,'analysis_workflow_run_id':os.environ['GITHUB_RUN_ID'],
        'source_commit':attempt['source_commit'],'release_sha256':permission.expected_sha256,**claim,'applications':reports,'main_estimate':None,
        'qualification_included_in_main':False,'four_distinct_research_hosts_verified':distinct,'all_required_application_audits_passed':passed and distinct}
    if claim['mode']=='main':summary['main_estimate']=day_summary(permission.artifacts,components,claim['day'],attempt['source_commit'],permission.expected_sha256)
    path=ROOT/('attempt-summary-'+args.run_id+'.json');path.write_text(json.dumps(summary,indent=2)+'\n');api.upload(release,path)
    print(json.dumps({'run_id':args.run_id,'mode':claim['mode'],'audited_applications':sum(r['status']=='passed' for r in reports),'qualification_included_in_main':False}))
    require(summary['all_required_application_audits_passed'],'one or more application audits unavailable; retained with planned weight')


if __name__=='__main__':main()
