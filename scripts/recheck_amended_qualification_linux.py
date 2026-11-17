"""Read-only Linux reconstruction of the existing third qualification; no acquisition."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from experiments.study_design.decision import require
from experiments.study_design.public_github import GitHub
from experiments.study_design.public_artifacts import download
from scripts.audit_amended_qualification import audit,RUN,REPLAY,SOURCE,RELEASE,CLAIM

REPO='shravaniparsi/query-rendering-benchmark'
BRANCH='offline-amended-qualification-recheck-v1'
def save(path,value):path.write_text(json.dumps(value,indent=2)+'\n')
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def run(output):
    require(platform.system()=='Linux' and platform.python_version()=='3.12.15' and os.environ.get('GITHUB_ACTIONS')=='true'
        and os.environ.get('RUNNER_OS')=='Linux' and os.environ.get('GITHUB_EVENT_NAME')=='workflow_dispatch','declared read-only Linux replay platform required')
    require(os.environ['GITHUB_REPOSITORY']==REPO and os.environ['GITHUB_REF']=='refs/heads/'+BRANCH
        and os.environ['GITHUB_WORKFLOW_REF']==REPO+'/.github/workflows/public-offline-diagnosis.yml@refs/heads/'+BRANCH,'isolated existing read-only workflow branch required')
    current=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip();require(current==os.environ['GITHUB_SHA'],'actual executing source differs')
    require(os.environ['QRL_FROZEN_EXECUTION_SHA']==SOURCE and os.environ['QRL_RELEASE_SHA256']==RELEASE
        and os.environ['QRL_MAIN_ENABLED']=='false' and os.environ['QRL_QUALIFICATION_ENABLED']=='true','frozen source and disabled main required before recheck')
    api=GitHub(REPO,os.environ['GH_TOKEN']);require(api.require_public()['id']==1447601875,'exact existing public repository required')
    output.mkdir(parents=True,exist_ok=False);inputs=output/'retained-inputs';inputs.mkdir()
    for folder,tag,count in [('raw','qualification-reserved-main-v2-run-'+RUN,8),('execution','execution-'+RUN,2),('replay','replay-'+RUN,5),('replay-execution','replay-execution-'+REPLAY,2)]:
        target=inputs/folder;target.mkdir();provider=api.call('GET','/releases/tags/'+tag)
        require(provider['target_commitish']==SOURCE and len(provider['assets'])==count,'existing original qualification release differs');save(target/'provider-release.json',provider)
        for asset in provider['assets']:download(api,provider,asset,target/asset['name'])
    save(inputs/'claim-tag.json',api.call('GET','/git/tags/'+CLAIM));save(inputs/'qualification-claim-refs.json',api.call('GET','/git/matching-refs/tags/qualification-reserved-main-v2-'))
    subprocess.run(['git','bundle','create',str((inputs/'public-source.bundle').resolve()),'refs/tags/reserved-main-v3-amended-frozen-source'],cwd=ROOT,check=True,capture_output=True)
    result=audit(inputs,output/'independent-replay');require(result['status']=='passed' and result['qualification_passed'] is True and result['own_nodes_verified_removed']==4
        and all(r['exact_paired_replay'] is True and r['cells']==66 for r in result['applications']),'both full application panels must replay exactly on Linux')
    report={'version':'actual-amended-qualification-linux-recheck-v1','status':'passed','recheck_run_id':os.environ['GITHUB_RUN_ID'],'recheck_source_commit':current,
        'parent_qualification_run_id':RUN,'frozen_execution_source':SOURCE,'release_sha256':RELEASE,'claim_sha':CLAIM,'provider_assets_verified':17,
        'exact_full_panels':['inventory','analytics'],'cells_per_application':66,'both_scales_verified':True,'own_node_removals_verified':4,
        'auditor_sha256':sha(ROOT/'scripts/audit_amended_qualification.py'),'driver_sha256':sha(Path(__file__)),
        'independent_audit_sha256':sha(output/'independent-replay/independent-qualification-audit.json'),
        'paired_audit_sha256':{task:sha(output/'independent-replay'/(task+'-independent-paired-audit.json')) for task in ('inventory','analytics')},
        'model_refit':False,'new_measurement_acquisition':False,'new_qualification_attempt':False,'main_enabled':False,
        'permissions':'contents:read; no artifact or release uploads','python':sys.version,'platform':platform.platform()}
    save(output/'linux-recheck-report.json',report);print('QRL_AMENDED_LINUX_RECHECK '+json.dumps(report,sort_keys=True),flush=True)
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',required=True,type=Path);args=parser.parse_args();run(args.output)


if __name__=='__main__':main()
