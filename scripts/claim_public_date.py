"""Atomically claim one predeclared real UTC date before four-runner acquisition."""
import argparse
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from experiments.study_design.decision import require
from experiments.study_design.performance_release import PerformancePermission
from experiments.study_design.public_github import GitHub


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--release',required=True);parser.add_argument('--release-sha256',required=True);parser.add_argument('--mode',choices=('qualification','main'),required=True);parser.add_argument('--day',default='');args=parser.parse_args()
    require(os.environ['GITHUB_RUN_ATTEMPT']=='1','re-runs cannot replace a date attempt');permission=PerformancePermission(ROOT,args.release,args.release_sha256);sample=permission.artifacts['sample_plan']
    require(os.environ.get('QRL_STANDARD_TAGGED_NETWORK_APPROVED')=='true' and os.environ.get('QRL_TS_CLIENT_ID') and os.environ.get('QRL_TS_AUDIENCE'),'approved free standard-tagged network identity must be configured before claiming a day')
    repo=os.environ['GITHUB_REPOSITORY'];api=GitHub(repo,os.environ['GH_TOKEN']);api.require_public();require(repo==sample['execution_repository'],'unexpected execution repository')
    source=os.environ['GITHUB_SHA'];require(source==os.environ['QRL_FROZEN_EXECUTION_SHA'],'workflow source differs from frozen execution commit')
    require(not subprocess.run(['git','status','--porcelain','--untracked-files=no'],cwd=ROOT,check=True,capture_output=True,text=True).stdout,'tracked source/artifact modifications')
    today=datetime.now(timezone.utc).date().isoformat();day=args.day or today;require(day==today,'claim must use actual real UTC date')
    dates=sample['collection_dates_utc'] if args.mode=='main' else sample.get('qualification_dates_utc',[])
    if day not in dates:
        require(not args.day and os.environ.get('GITHUB_EVENT_NAME')=='schedule','unplanned manually requested collection date')
        with Path(os.environ['GITHUB_OUTPUT']).open('a') as stream:stream.write('acquire=false\n')
        print(json.dumps({'status':'outside fixed calendar','acquisition_started':False}));return
    campaign=sample['campaign_id'];require(re.fullmatch('[a-z0-9-]{1,40}',campaign),'bounded campaign identity required');prefix=campaign if args.mode=='main' else 'qualification-'+campaign
    name=prefix+'-'+day;reference='/git/refs';message={'collection_date_utc':day,'mode':args.mode,'run_id':os.environ['GITHUB_RUN_ID'],'run_attempt':'1','source_commit':source,'release_sha256':args.release_sha256}
    require(api.optional('/git/ref/tags/'+name) is None,'calendar date already claimed; do not replace or re-run')
    tag=api.call('POST','/git/tags',value={'tag':name,'message':json.dumps(message,sort_keys=True),'object':source,'type':'commit'})
    # Ref creation is atomic. A concurrent attempt that loses this race stops here.
    api.call('POST',reference,value={'ref':'refs/tags/'+name,'sha':tag['sha']})
    release_tag=campaign if args.mode=='main' else prefix+'-run-'+os.environ['GITHUB_RUN_ID']
    output={'acquire':'true','day':day,'mode':args.mode,'claim_sha':tag['sha'],'release_tag':release_tag}
    with Path(os.environ['GITHUB_OUTPUT']).open('a') as stream:
        for key,value in output.items():stream.write(key+'='+value+'\n')
    print('QRL_CALENDAR_CLAIM '+json.dumps({**output,'run_id':os.environ['GITHUB_RUN_ID'],'source_commit':source,'release_sha256':args.release_sha256},sort_keys=True))


if __name__=='__main__':main()
