"""Remove only recorded disposable nodes from one retained failed network check."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
from urllib.parse import quote
from urllib.request import urlopen

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from experiments.study_design.decision import require
from experiments.study_design.public_github import GitHub
from scripts.bundle_public_output import bundle
from scripts.public_network_check import context
from scripts.research_network import TAG,access_token,api as device_api,cleanup

FAILED_RUN='42139611043'
FAILED_SOURCE='d3afe60f396ea1ba7346b316a9e405b50d00fbe9'


def download(asset,tag,repository):
    expected=f'https://github.com/{repository}/releases/download/{tag}/'+quote(asset['name'],safe='')
    require(asset['browser_download_url']==expected and 0<asset['size']<1024*1024,'bounded exact original release asset required')
    # Public evidence download carries no Authorization header across redirects.
    with urlopen(expected,timeout=60) as response:raw=response.read(1024*1024+1)
    require(len(raw)==asset['size'] and asset['digest']=='sha256:'+hashlib.sha256(raw).hexdigest(),'original public asset digest differs')
    return raw


def role_files(raw,manifest,task,role):
    require(manifest['sha256']==hashlib.sha256(raw).hexdigest() and manifest['bytes']==len(raw),'original role bundle differs')
    expected={r['path']:r for r in manifest['files']};require(len(expected)==len(manifest['files']),'duplicate original role file')
    result={}
    with tarfile.open(fileobj=io.BytesIO(raw),mode='r:gz') as source:
        members=source.getmembers();require(len(members)==len(expected),'original member count differs')
        for member in members:
            require(member.isfile() and member.name in expected and not Path(member.name).is_absolute() and '..' not in Path(member.name).parts,'unsafe original role member')
            data=source.extractfile(member).read();row=expected.pop(member.name)
            require(len(data)==row['bytes'] and hashlib.sha256(data).hexdigest()==row['sha256'],'original role file digest differs')
            result[member.name]=data
    row=json.loads(result['public-network-identity.json'])
    require(row['run_id']==FAILED_RUN and row['source_commit']==FAILED_SOURCE and row['run_attempt']=='1' and row['task']==task and row['role']==role and row['benchmark_measurements'] is False,'original role identity differs')
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--failed-run',required=True);args=parser.parse_args()
    require(args.failed_run==FAILED_RUN,'recovery is restricted to the one recorded failed engineering attempt')
    github=GitHub(os.environ['GITHUB_REPOSITORY'],os.environ['GH_TOKEN']);current=context(github,os.environ)
    run=github.call('GET','/actions/runs/'+FAILED_RUN)
    require(str(run['id'])==FAILED_RUN and run['head_sha']==FAILED_SOURCE and run['run_attempt']==1 and run['status']=='completed' and run['conclusion']=='failure'
        and run['path']=='.github/workflows/public-main-evaluation.yml' and run['head_branch']=='main' and run['head_repository']['full_name']==github.repository,'wrong original failed run')
    release=github.call('GET','/releases/tags/network-check-'+FAILED_RUN);require(release['target_commitish']==FAILED_SOURCE,'wrong original release source')
    assets={a['name']:a for a in release['assets']};require(len(assets)==8,'all four original role bundles and manifests required')
    output=ROOT/'ci-artifacts';output.mkdir(exist_ok=True);rows=[];failures=[]
    try:
        for task in ('inventory','analytics'):
            for role in ('target','observer'):
                name=f'qrl-{FAILED_RUN}{"0" if task=="inventory" else "1"}-1-{role}';directory=output/(task+'-'+role);directory.mkdir()
                try:
                    stem=f'network-check-{FAILED_RUN}-1-{task}-{role}';manifest=json.loads(download(assets[stem+'.json'],release['tag_name'],github.repository))
                    files=role_files(download(assets[stem+'.tar.gz'],release['tag_name'],github.repository),manifest,task,role)
                    if 'network-registration.json' in files:
                        record=json.loads(files['network-registration.json']);require(record['hostname']==name and record['tag']==TAG,'original ownership record differs')
                        path=directory/'network-registration.json';path.write_bytes(files['network-registration.json']);cleanup(path,name)
                        rows.append({'task':task,'role':role,'status':'recorded node verified absent','node_id':record['node_id']})
                    else:
                        devices=device_api('tailnet/-/devices?fields=all',token=access_token())['devices']
                        require(not any(d.get('hostname')==name for d in devices),'unrecorded matching node requires separate review; no deletion attempted')
                        rows.append({'task':task,'role':role,'status':'no registration record and matching node absent'})
                except Exception as error:failures.append({'task':task,'role':role,'error_type':type(error).__name__,'error':str(error)[:250]})
    finally:
        report={**current,'purpose':'exact retained failed-attempt cleanup only; no node registration','failed_run_id':FAILED_RUN,'failed_source_commit':FAILED_SOURCE,'rows':rows,'failures':failures,
            'status':'failed' if failures else 'passed'}
        (output/'network-recovery.json').write_text(json.dumps(report,indent=2)+'\n')
        stem='network-recovery-'+current['run_id'];archive=Path('/tmp')/(stem+'.tar.gz');manifest=Path('/tmp')/(stem+'.json');bundle(output,archive,manifest)
        identity=hashlib.sha256(json.dumps(current,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        retained=github.ensure_release(stem,current['source_commit'],identity)
        for path in (archive,manifest):github.upload(retained,path)
    require(not failures,'some original research nodes still require attention; see retained recovery evidence')
    print(json.dumps({'status':'passed','verified_original_roles':len(rows),'new_nodes_registered':0}))


if __name__=='__main__':main()
