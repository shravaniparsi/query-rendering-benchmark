"""Retain each completed or interrupted role as immutable public release assets."""
import argparse
import json
import os
from pathlib import Path
import re
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from experiments.study_design.decision import require
from experiments.study_design.public_github import GitHub,verify_claim
from scripts.bundle_public_output import bundle


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('task','role','day','mode','claim-sha','release-sha256','release-tag'):parser.add_argument('--'+name,required=True)
    args=parser.parse_args();require(args.task in ('inventory','analytics') and args.role in ('target','observer') and args.mode in ('qualification','main'),'known acquisition role required')
    api=GitHub(os.environ['GITHUB_REPOSITORY'],os.environ['GH_TOKEN']);api.require_public();source=os.environ['GITHUB_SHA'];run=os.environ['GITHUB_RUN_ID'];attempt=os.environ['GITHUB_RUN_ATTEMPT']
    require(attempt=='1' and source==os.environ['QRL_FROZEN_EXECUTION_SHA'],'unexpected replacement/source');verify_claim(api,args.claim_sha,day=args.day,mode=args.mode,run_id=run,source=source,release_sha=args.release_sha256)
    output=ROOT/'ci-artifacts';output.mkdir(exist_ok=True);(output/'public-role-identity.json').write_text(json.dumps({'task':args.task,'role':args.role,'day':args.day,'mode':args.mode,'source_commit':source,'run_id':run,'run_attempt':attempt,
        'claim_sha':args.claim_sha,'release_sha256':args.release_sha256,'collection_step_outcome':os.environ['QRL_COLLECTION_STEP_OUTCOME']},indent=2)+'\n')
    stem=f'{args.day}-{args.task}-{args.role}-{run}-{attempt}';archive=Path('/tmp')/(stem+'.tar.gz');manifest=Path('/tmp')/(stem+'.json');value=bundle(output,archive,manifest)
    release=api.ensure_release(args.release_tag,source,args.release_sha256);assets=[api.upload(release,path) for path in (archive,manifest)]
    print(json.dumps({'status':'retained','run_id':run,'task':args.task,'role':args.role,'bytes':value['bytes'],'release_tag':args.release_tag,'assets':[{'name':a['name'],'digest':a['digest']} for a in assets]}))


if __name__=='__main__':main()
