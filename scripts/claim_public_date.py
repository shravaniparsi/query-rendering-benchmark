"""Atomically claim one predeclared real UTC date before four-runner acquisition."""
import argparse
import base64
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from experiments.study_design.decision import require
from experiments.study_design.performance_release import PerformancePermission
from experiments.study_design.public_github import GitHub,verify_claim


def qualification_block(api,sample,day,source,sources):
    """A failed source cannot consume the next date without a code correction."""
    if sample.get('qualification_attempt_limit') is None:return None
    dates=sample['qualification_budget_dates_utc'];require(sample['qualification_attempt_limit']==2 and len(dates)==2 and dates==sorted(set(dates)),'fixed two-date qualification budget required')
    history=[]
    for date in dates:
        if date>day:continue
        reference=api.optional('/git/ref/tags/qualification-'+sample['campaign_id']+'-'+date)
        if reference is None:continue
        require(reference['object']['type']=='tag','immutable annotated qualification history required')
        tag=api.call('GET','/git/tags/'+reference['object']['sha']);message=json.loads(tag['message'])
        verify_claim(api,reference['object']['sha'],day=date,mode='qualification',run_id=message['run_id'],source=message['source_commit'],release_sha=message['release_sha256'])
        history.append(message)
    if len(history)>=2:return 'fixed qualification attempt budget exhausted'
    for row in history:
        if row['source_commit']==source:return 'qualification already attempted from this execution source'
        item=api.call('GET','/contents/release/release.json?ref='+row['source_commit']);require(item['encoding']=='base64','original actual release contents required')
        raw=base64.b64decode(item['content'].replace('\n',''),validate=True)
        require(hashlib.sha256(raw).hexdigest()==row['release_sha256'],'original qualification release digest differs')
        if json.loads(raw)['source_files']==sources:return 'qualification retry requires a tested source correction; artifact-only changes are insufficient'
    return None


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
    if args.mode=='qualification':
        reason=qualification_block(api,sample,day,source,permission._source_sha)
        if reason:
            with Path(os.environ['GITHUB_OUTPUT']).open('a') as stream:stream.write('acquire=false\n')
            print(json.dumps({'status':reason,'acquisition_started':False}));return
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
