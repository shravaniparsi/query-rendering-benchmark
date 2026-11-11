"""Build and verify actual reserved release files on the public Linux fit platform."""
import argparse
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from experiments.study_design.artifact_release import VERSION
from experiments.study_design.decision import require
from experiments.study_design.performance_release import PerformancePermission
from experiments.study_design.public_github import GitHub
from experiments.study_design.release_preparation import protocols
from scripts.claim_public_date import qualification_block

REPO='shravaniparsi/query-rendering-benchmark'
PROTOCOL='paper/followup/query-rendering-load/protocol/'
MODEL='data/decision-artifact/development-v1-1/model.json'
TRAINING='data/endtoend-distribution/v1-1/training-inputs.json'


def sha(raw):return hashlib.sha256(raw).hexdigest()
def git(*args):return subprocess.run(['git',*args],cwd=ROOT,check=True,capture_output=True).stdout
def descriptor(name):
    raw=(ROOT/name).read_bytes();return {'path':name,'bytes':len(raw),'sha256':sha(raw)}
def write(name,value):
    path=ROOT/name;require(not path.exists() and not path.is_symlink(),'new actual release files required');path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,sort_keys=True,indent=2)+'\n');return descriptor(name)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--calendar-env',default='QRL_CALENDAR_JSON');args=parser.parse_args()
    require(platform.system()=='Linux' and platform.python_version()=='3.12.15' and os.environ.get('GITHUB_ACTIONS')=='true'
        and os.environ.get('RUNNER_OS')=='Linux' and os.environ.get('GITHUB_EVENT_NAME')=='workflow_dispatch','declared public Linux/Python preparation platform required')
    require(os.environ['GITHUB_REPOSITORY']==REPO and os.environ['GITHUB_REF']=='refs/heads/main'
        and os.environ['GITHUB_WORKFLOW_REF']==REPO+'/.github/workflows/public-release-prepare.yml@refs/heads/main','dedicated main-branch release preparation workflow required')
    api=GitHub(REPO,os.environ['GH_TOKEN']);repository=api.require_public();require(repository['id']==1447601875,'unexpected public repository')
    source=git('rev-parse','HEAD').decode().strip();require(source==os.environ['GITHUB_SHA'] and re.fullmatch('[0-9a-f]{40}',source),'exact executing source commit required')
    require(not git('status','--porcelain','--untracked-files=no'),'tracked source changed before preparation')
    require(git('rev-list','--max-parents=0',source).decode().splitlines()==['b2c396b2b769b29308bc16b80cbc26de18b57d65'],'isolated public Git history required')
    require(not (ROOT/'release').exists(),'new release directory required')
    for name,expected in ((MODEL,'6754de7d8f2cf01f4f575651d86f8f9edda50bd91e48fb6aa51b0f9c48f287cd'),(TRAINING,'7d1e58aa4d1508de31acd711c759022286f9b30580fe8b2973c866688d3e5839')):
        require(sha((ROOT/name).read_bytes())==sha(git('show',source+':'+name))==expected,'unchanged actual model/training bytes required')
    calendar=json.loads(os.environ[args.calendar_env]);registry=json.loads((ROOT/(PROTOCOL+'application_reservation_v1.json')).read_bytes())
    old_contract=json.loads((ROOT/(PROTOCOL+'main_decision_components_v1.json')).read_bytes())
    values=protocols(calendar,registry,old_contract,today=datetime.now(timezone.utc).date())
    artifacts={'model':descriptor(MODEL),'training_inputs':descriptor(TRAINING),'registry':descriptor(PROTOCOL+'application_reservation_v1.json')}
    for name in ('profile_protocol','main_contexts','decision_contract'):artifacts[name]=write('release/'+name+'.json',values[name])
    for field,name in (('model_sha256','model'),('profile_protocol_sha256','profile_protocol'),('main_contexts_sha256','main_contexts')):
        values['sample_plan'][field]=artifacts[name]['sha256']
    artifacts['sample_plan']=write('release/sample_plan.json',values['sample_plan']);write('release/calendar_input.json',calendar)
    tracked=git('ls-tree','-r','--name-only',source).decode().splitlines()
    paths=[p for p in tracked if (p.startswith(('experiments/','scripts/','apps/','tests/','.github/workflows/')) and Path(p).suffix in ('.py','.cjs','.js','.html','.json','.txt','.yml')) or p.startswith(PROTOCOL)]
    sources={}
    for name in paths:
        raw=(ROOT/name).read_bytes();require(sha(raw)==sha(git('show',source+':'+name)),'working/committed source differs: '+name);sources[name]=sha(raw)
    model=json.loads((ROOT/MODEL).read_bytes());envelope={'version':VERSION,'model_source_commit':source,'artifacts':artifacts,'source_files':sources,
        'fit_applications':model['fit_applications'],'hyperparameters_frozen':True}
    released=write('release/release.json',envelope);permission=PerformancePermission(ROOT,released['path'],released['sha256'])
    out=ROOT/'ci-artifacts';out.mkdir(exist_ok=True)
    if calendar['version']=='reserved-calendar-plan-v3':
        require(qualification_block(api,permission.artifacts['sample_plan'],calendar['qualification_dates_utc'][0],source,sources) is None,'amended qualification history is blocked')
        history={'status':'passed','role':'actual prior-claim verification only; no date claimed','total_limit':3,'prior_claim_count':2,
            'campaign_id':calendar['campaign_id'],'amendment':calendar['qualification_amendment'],
            'actual_qualification_refs':api.call('GET','/git/matching-refs/tags/qualification-'+calendar['campaign_id']+'-')}
        (out/'qualification-history-check.json').write_text(json.dumps(history,indent=2)+'\n')
    from experiments.heldout_tasks.main_design import cells
    counts={task:{'main_cases':len(cells(permission.artifacts,task,calendar['collection_dates_utc'][0])),
        'qualification_cases':len(cells(permission.artifacts,task,calendar['qualification_dates_utc'][0],True))} for task in ('inventory','analytics')}
    report={k:v for k,v in permission.checked.items() if k!='artifacts'}
    report.update(role='actual seven-artifact candidate verification and exact unchanged development-fit replay; no acquisition',run_id=os.environ['GITHUB_RUN_ID'],
        source_commit=source,python=sys.version,platform=platform.platform(),release_file=released,case_counts=counts,benchmark_measurements=False,
        main_enabled=False,qualification_enabled=False,calendar_input_sha256=sha(json.dumps(calendar,sort_keys=True,separators=(',',':')).encode()))
    (out/'release-validation.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'status':'passed','actual_artifacts_checked':7,'release_sha256':released['sha256'],'main_acquisition_started':False}))


if __name__=='__main__':main()
