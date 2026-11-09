"""Replay existing failed qualification evidence on Linux without new acquisition."""
import argparse
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from experiments.study_design.decision import require
from experiments.study_design.public_artifacts import download
from experiments.study_design.public_github import GitHub
from scripts.audit_final_qualification_failure import RUN,REPLAY,SOURCE,RELEASE
from scripts.diagnose_final_qualification import diagnose
from scripts.verify_final_policy_commitments import verify

REPO='shravaniparsi/query-rendering-benchmark'
def save(path,value):path.write_text(json.dumps(value,indent=2)+'\n')
def git(*args):return subprocess.check_output(['git',*args],cwd=ROOT)


def run(output):
    require(platform.system()=='Linux' and platform.python_version()=='3.12.15' and os.environ.get('GITHUB_ACTIONS')=='true'
        and os.environ.get('RUNNER_OS')=='Linux' and os.environ.get('GITHUB_EVENT_NAME')=='workflow_dispatch','declared public Linux offline replay platform required')
    require(os.environ['GITHUB_REPOSITORY']==REPO and os.environ['GITHUB_REF']=='refs/heads/main'
        and os.environ['GITHUB_WORKFLOW_REF']==REPO+'/.github/workflows/public-offline-diagnosis.yml@refs/heads/main','dedicated main-branch offline replay workflow required')
    api=GitHub(REPO,os.environ['GH_TOKEN']);repository=api.require_public();require(repository['id']==1447601875,'unexpected public repository')
    current=git('rev-parse','HEAD').decode().strip();require(current==os.environ['GITHUB_SHA'],'exact executing replay source required')
    require(git('rev-list','--max-parents=0',current).decode().splitlines()==['b2c396b2b769b29308bc16b80cbc26de18b57d65'],'isolated public history required')
    gates={name:os.environ.get(name) for name in ('QRL_MAIN_ENABLED','QRL_QUALIFICATION_ENABLED','QRL_FROZEN_EXECUTION_SHA','QRL_RELEASE_SHA256')}
    require(gates=={'QRL_MAIN_ENABLED':'false','QRL_QUALIFICATION_ENABLED':'false','QRL_FROZEN_EXECUTION_SHA':SOURCE,'QRL_RELEASE_SHA256':RELEASE},'acquisition must remain disabled at the original frozen source')
    output.mkdir(parents=True,exist_ok=False);inputs=output/'retained-inputs';inputs.mkdir()
    save(output/'offline-run-identity.json',{'source_commit':current,'run_id':os.environ['GITHUB_RUN_ID'],'parent_qualification_run_id':RUN,
        'original_frozen_source':SOURCE,'release_sha256':RELEASE,'python':sys.version,'platform':platform.platform(),'role':'offline evidence replay only',
        'acquisition_started':False,'model_refit':False,'qualification_passed':False,'main_component_produced':False,'main_enabled':False,'qualification_enabled':False,
        'started_at_utc':datetime.now(timezone.utc).isoformat()})
    retained=0
    for folder,tag,count in [('raw','qualification-reserved-main-v2-run-'+RUN,8),('execution','execution-'+RUN,2),('replay','replay-'+RUN,5),('replay-execution','replay-execution-'+REPLAY,2)]:
        directory=inputs/folder;directory.mkdir();provider=api.call('GET','/releases/tags/'+tag)
        require(provider['target_commitish']==SOURCE and len(provider['assets'])==count,'complete original source-bound assets required')
        save(directory/'provider-release.json',provider)
        for asset in provider['assets']:download(api,provider,asset,directory/asset['name']);retained+=1
    save(inputs/'claim-tag.json',api.call('GET','/git/tags/09f162d6d38fe0e4f6361bf5bd49c75aac86feb0'))
    save(inputs/'qualification-claim-refs.json',api.call('GET','/git/matching-refs/tags/qualification-reserved-main-v2-'))
    save(inputs/'gate-readback.json',[{'name':key,'value':value} for key,value in sorted(gates.items())])
    subprocess.run(['git','bundle','create',str((inputs/'public-source.bundle').resolve()),'refs/tags/reserved-main-v2-browser-fix-frozen-source'],cwd=ROOT,check=True,capture_output=True)
    save(output/'policy-commitment-check.json',verify(inputs))
    result=diagnose(inputs,output/'diagnostics');save(output/'linux-replay-result.json',result)
    require(retained==17 and result['inventory_full_66_cell_diagnostic_passed'] is True and result['inventory_followup'] is None,
        'corrected full inventory replay is not exact on the declared Linux platform')
    require(result['analytics_cell_diagnostic_passed'] is True and result['wrong_paint_text_rejected']['reproduced'] is True
        and result['analytics_full_panel_completed'] is False and result['qualification_passed'] is False
        and result['main_component_produced'] is result['refit_performed'] is result['acquisition_started'] is False,'offline diagnosis cannot complete qualification or enter main')
    report={'version':'actual-linux-offline-qualification-replay-v1','status':'passed','replay_source_commit':current,'run_id':os.environ['GITHUB_RUN_ID'],
        'parent_qualification_run_id':RUN,'original_frozen_source':SOURCE,'release_sha256':RELEASE,'provider_assets_verified':retained,
        'inventory_exact_corrected_66_cell_replay':True,'analytics_corrected_cell_replay':'l-002','false_paint_content_rejected':True,
        'both_preloaded_commitments_verified':True,'qualification_passed':False,'main_component_produced':False,'model_refit':False,'acquisition_started':False,
        'driver_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'python':sys.version,'platform':platform.platform()}
    save(output/'linux-offline-verification.json',report);return report


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    print(json.dumps(run(args.output)))


if __name__=='__main__':main()
