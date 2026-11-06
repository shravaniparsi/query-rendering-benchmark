"""Resolve one actual source-bound calendar claim after its Actions run completes."""
import argparse
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import re
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from experiments.study_design.decision import require
from experiments.study_design.performance_release import PerformancePermission
from experiments.study_design.public_github import GitHub,verify_claim


def inspect(api,permission,run_id,source):
    require(run_id.isdigit() and re.fullmatch('[0-9a-f]{40}',source),'actual parent run and frozen execution source required')
    api.require_public();run=api.call('GET','/actions/runs/'+run_id)
    require(str(run['id'])==run_id and run['status']=='completed' and run['head_sha']==source and run['run_attempt']==1 and run['head_branch']=='main'
        and run['head_repository']['full_name']==api.repository and run['path']=='.github/workflows/public-main-evaluation.yml'
        and run['event'] in ('workflow_dispatch','schedule'),'wrong completed first acquisition attempt/source')
    sample=permission.artifacts['sample_plan'];require(api.repository==sample['execution_repository'],'wrong public execution repository')
    first=datetime.fromisoformat(run['created_at'].replace('Z','+00:00')).astimezone(timezone.utc).date().isoformat()
    last=datetime.fromisoformat(run['updated_at'].replace('Z','+00:00')).astimezone(timezone.utc).date().isoformat();matches=[]
    for mode,days in (('main',sample['collection_dates_utc']),('qualification',sample.get('qualification_dates_utc',[]))):
        campaign=sample['campaign_id'];prefix=campaign if mode=='main' else 'qualification-'+campaign
        for day in days:
            if not first<=day<=last:continue
            reference=api.optional('/git/ref/tags/'+prefix+'-'+day)
            if reference is None:continue
            require(reference['object']['type']=='tag','annotated calendar claim required');claim_sha=reference['object']['sha'];tag=api.call('GET','/git/tags/'+claim_sha)
            message=json.loads(tag['message'])
            if message.get('run_id')!=run_id:continue
            verify_claim(api,claim_sha,day=day,mode=mode,run_id=run_id,source=source,release_sha=permission.expected_sha256)
            matches.append({'day':day,'mode':mode,'claim_sha':claim_sha,'release_tag':campaign if mode=='main' else prefix+'-run-'+run_id})
    require(len(matches)<=1,'one Actions run cannot claim multiple calendar dates')
    return {'run_id':run_id,'source_commit':source,'release_sha256':permission.expected_sha256,'parent_run':run,'claim':matches[0] if matches else None}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--run-id',required=True);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    permission=PerformancePermission(ROOT,'release/release.json',os.environ['QRL_RELEASE_SHA256']);api=GitHub(os.environ['GITHUB_REPOSITORY'],os.environ['GH_TOKEN'])
    result=inspect(api,permission,args.run_id,os.environ['QRL_FROZEN_EXECUTION_SHA']);args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(result,indent=2)+'\n')
    with Path(os.environ['GITHUB_OUTPUT']).open('a') as stream:
        stream.write('claimed='+('true' if result['claim'] else 'false')+'\n')
        if result['claim']:
            for key,value in result['claim'].items():stream.write(key+'='+value+'\n')
    print(json.dumps({'status':'resolved','run_id':args.run_id,'claim':result['claim']}))


if __name__=='__main__':main()
