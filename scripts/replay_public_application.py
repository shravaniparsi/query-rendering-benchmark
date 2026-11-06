"""Download, replay and retain one claimed application's complete paired evidence."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from experiments.study_design.decision import require
from experiments.study_design.performance_release import PerformancePermission
from experiments.study_design.public_github import GitHub
from experiments.study_design.public_artifacts import assets,download,extract
from experiments.study_design.main_analysis import component
from scripts.inspect_public_attempt import inspect
from scripts.bundle_public_output import bundle
from scripts.research_network import verify_key_receipt,VERSION,BINARY_SHA


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--run-id',required=True);parser.add_argument('--task',choices=('inventory','analytics'),required=True);args=parser.parse_args()
    permission=PerformancePermission(ROOT,'release/release.json',os.environ['QRL_RELEASE_SHA256']);api=GitHub(os.environ['GITHUB_REPOSITORY'],os.environ['GH_TOKEN'])
    attempt=inspect(api,permission,args.run_id,os.environ['QRL_FROZEN_EXECUTION_SHA']);require(attempt['claim'] is not None,'actual claimed acquisition required')
    claim=attempt['claim'];source=attempt['source_commit'];output=ROOT/'ci-artifacts';output.mkdir(exist_ok=True)
    status={'version':'public-application-replay-v1','status':'unavailable','task':args.task,'run_id':args.run_id,'source_commit':source,
        'release_sha256':permission.expected_sha256,**claim,'analysis_workflow_run_id':os.environ['GITHUB_RUN_ID'],'roles':[]}
    failure=None
    try:
        release=api.call('GET','/releases/tags/'+claim['release_tag'])
        require(release['target_commitish']==source and permission.expected_sha256 in release['body'],'raw evidence release belongs to another source/envelope')
        inventory=assets(api,release)
        with tempfile.TemporaryDirectory(prefix='qrl-paired-replay-') as temp:
            directory=Path(temp);paths={};manifests={}
            for role in ('target','observer'):
                stem=f'{claim["day"]}-{args.task}-{role}-{args.run_id}-1';records=[]
                for suffix in ('.json','.tar.gz'):
                    name=stem+suffix;record=inventory[name];download(api,release,record,directory/name);records.append({k:record[k] for k in ('name','size','digest')})
                manifests[role]=json.loads((directory/(stem+'.json')).read_bytes());paths[role]=(stem,directory/role)
                status['roles'].append({'role':role,'assets':records})
            expanded=sum(row['bytes'] for value in manifests.values() for row in value['files'])
            require(expanded<shutil.disk_usage(directory).free-1024**3,'paired evidence exceeds free replay workspace; raw assets remain retained')
            for role,(stem,path) in paths.items():
                extract(directory/(stem+'.tar.gz'),directory/(stem+'.json'),path);identity=json.loads((path/'public-role-identity.json').read_bytes())
                expected={'task':args.task,'role':role,'day':claim['day'],'mode':claim['mode'],'source_commit':source,'run_id':args.run_id,'run_attempt':'1',
                    'claim_sha':claim['claim_sha'],'release_sha256':permission.expected_sha256}
                require(all(identity[k]==v for k,v in expected.items()) and identity['collection_step_outcome']=='success','raw role identity or collection outcome differs')
                registration=json.loads((path/'network-registration.json').read_bytes());cleanup=json.loads((path/'network-cleanup.json').read_bytes())
                name=f'qrl-{args.run_id}{0 if args.task=="inventory" else 1}-1-{role}';receipt=json.loads((path/'registration-auth-key.json').read_bytes());verify_key_receipt(receipt,name)
                require(registration['hostname']==name and registration['tag']=='tag:qrl-main-ci' and registration['registration_auth_key_id']==receipt['key_id']
                    and registration['client_version']==VERSION and registration['client_tarball_sha256']==BINARY_SHA and registration['provider_is_ephemeral'] is not True
                    and registration['verified_standard_node'] is True and cleanup=={'status':'removed','hostname':name,'node_id':registration['node_id'],'tag':'tag:qrl-main-ci','verified_absent':True},'ordinary pinned own-node registration/cleanup differs')
                local=json.loads((path/('main-'+args.task)/'report.json').read_bytes())['local'];require(local['overlay_name']==name,'native overlay identity differs')
                status['roles'][-2 if role=='target' else -1].update(node_id=registration['node_id'],kernel_boot_sha256=local['kernel_boot_sha256'])
            from scripts.audit_main_evaluation import audit
            audited=audit(paths['target'][1]/('main-'+args.task),paths['observer'][1]/('main-'+args.task),permission)
            require(audited['source_commit']==source and audited['github_run_id']==args.run_id and audited['collection_date_utc']==claim['day'] and audited['smoke']==(claim['mode']=='qualification'),'audited parent identity differs')
            (output/'paired-audit.json').write_text(json.dumps(audited,indent=2)+'\n')
            if claim['mode']=='main':(output/'component.json').write_text(json.dumps(component(audited,permission.artifacts,claim['claim_sha']),indent=2)+'\n')
            status['status']='passed';status['paired_auditor_sha256']=hashlib.sha256((ROOT/'scripts/audit_main_evaluation.py').read_bytes()).hexdigest()
    except Exception as error:
        failure=error;status.update(error_type=type(error).__name__,error=str(error)[:500])
    finally:
        (output/'application-replay.json').write_text(json.dumps(status,indent=2)+'\n')
        stem=f'application-replay-{args.run_id}-{args.task}';archive=Path('/tmp')/(stem+'.tar.gz');manifest=Path('/tmp')/(stem+'.json');bundle(output,archive,manifest)
        release=api.ensure_release('replay-'+args.run_id,source,permission.expected_sha256)
        for path in (archive,manifest):api.upload(release,path)
    if failure:raise failure
    print(json.dumps({'status':'passed','task':args.task,'mode':claim['mode'],'run_id':args.run_id}))


if __name__=='__main__':main()
