"""Verify and retain a manual four-runner network-only check, without study data."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from experiments.study_design.decision import require
from experiments.study_design.public_github import GitHub
from scripts.bundle_public_output import bundle

REPOSITORY='shravaniparsi/query-rendering-benchmark'
WORKFLOW=REPOSITORY+'/.github/workflows/public-main-evaluation.yml@refs/heads/main'


def context(api,env):
    source=env['GITHUB_SHA'];run=env['GITHUB_RUN_ID']
    require(api.repository==REPOSITORY and env['GITHUB_REPOSITORY']==REPOSITORY,'dedicated public execution repository required')
    require(re.fullmatch('[0-9a-f]{40}',source) and source==env.get('QRL_NETWORK_EXECUTION_SHA'),'network check must use its separately recorded source commit')
    require(run.isdigit() and env['GITHUB_RUN_ATTEMPT']=='1','new first-attempt network check required')
    require(env.get('GITHUB_EVENT_NAME')=='workflow_dispatch' and env.get('GITHUB_REF')=='refs/heads/main' and env.get('GITHUB_WORKFLOW_REF')==WORKFLOW,'manual main-branch network workflow required')
    require(env.get('QRL_STANDARD_TAGGED_NETWORK_APPROVED')=='true','approved dedicated research identity required')
    repository=api.require_public();require(repository['id']==1447601875,'public repository identity differs')
    return {'version':'qrl-public-network-check-v1','repository':REPOSITORY,'source_commit':source,'run_id':run,'run_attempt':'1',
        'purpose':'four-runner OIDC, direct connectivity and own-node cleanup only','benchmark_measurements':False,'additional_spending_limit':0}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=('verify','publish'))
    parser.add_argument('--task',choices=('inventory','analytics'),required=True);parser.add_argument('--role',choices=('target','observer'),required=True)
    args=parser.parse_args();api=GitHub(os.environ['GITHUB_REPOSITORY'],os.environ['GH_TOKEN']);identity=context(api,os.environ)
    output=ROOT/'ci-artifacts';output.mkdir(exist_ok=True)
    if args.action=='verify':
        (output/'network-check-permission.json').write_text(json.dumps(identity,indent=2)+'\n');print(json.dumps(identity));return
    row={**identity,'task':args.task,'role':args.role,'registration_step':os.environ['QRL_REGISTRATION_OUTCOME'],
        'connectivity_step':os.environ['QRL_CONNECTIVITY_OUTCOME'],'cleanup_step':os.environ['QRL_CLEANUP_OUTCOME']}
    (output/'public-network-identity.json').write_text(json.dumps(row,indent=2)+'\n')
    stem=f'network-check-{identity["run_id"]}-1-{args.task}-{args.role}'
    archive=Path('/tmp')/(stem+'.tar.gz');manifest=Path('/tmp')/(stem+'.json');value=bundle(output,archive,manifest)
    common_sha=hashlib.sha256(json.dumps(identity,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    release=api.ensure_release('network-check-'+identity['run_id'],identity['source_commit'],common_sha)
    assets=[api.upload(release,path) for path in (archive,manifest)]
    print(json.dumps({'status':'retained','benchmark_measurements':False,'members':len(value['files']),'assets':[{'name':a['name'],'digest':a['digest']} for a in assets]}))


if __name__=='__main__':main()
