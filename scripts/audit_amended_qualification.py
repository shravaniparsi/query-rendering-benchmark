"""Independently reconstruct the actual amended full qualification, without refitting."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from experiments.study_design.decision import require
from experiments.study_design.public_artifacts import extract
from experiments.heldout_tasks.performance_evidence import digest
from scripts.research_network import verify_key_receipt,TAG,VERSION,BINARY_SHA

RUN='43473084067';REPLAY='43480338805';DAY='2026-11-13'
SOURCE='bbb0dfdf1209eb77201c4823ec4edbce5240ecf4'
MODEL_SOURCE='a5970603b779f136b2037b9a4967325b02331607'
RELEASE='02e15de76ae6c4b62b10693d7e615a4d2b46fd80c7710671e346fe6dccb2666c'
CLAIM='f452a99dcf74b541ddd32e2a13f43c0caff12f50'
def sha(raw):return hashlib.sha256(raw).hexdigest()
def load(path):return json.loads(path.read_bytes())
def save(path,value):path.write_text(json.dumps(value,indent=2)+'\n')


def audit(directory,output):
    output.mkdir(exist_ok=False);asset_count=0
    for folder,tag,count in [('raw','qualification-reserved-main-v2-run-'+RUN,8),('execution','execution-'+RUN,2),('replay','replay-'+RUN,5),('replay-execution','replay-execution-'+REPLAY,2)]:
        provider=load(directory/folder/'provider-release.json')
        require(provider['tag_name']==tag and provider['target_commitish']==SOURCE and len(provider['assets'])==len({r['name'] for r in provider['assets']})==count,'complete source-bound provider inventory required')
        for row in provider['assets']:
            require(Path(row['name']).name==row['name'],'confined provider asset required');raw=(directory/folder/row['name']).read_bytes()
            require(len(raw)==row['size'] and row['digest']=='sha256:'+sha(raw),'provider digest differs');asset_count+=1
    metadata=load(directory/'execution'/('public-execution-'+RUN+'-1-metadata.json'))
    require(str(metadata['databaseId'])==RUN and metadata['headSha']==SOURCE and metadata['status']=='completed' and metadata['conclusion']=='success' and metadata['event']=='schedule','actual completed scheduled qualification required')
    jobs={r['name']:r for r in metadata['jobs'] if r['name'].startswith('collection (')};require(len(jobs)==4,'four actual collection roles required')
    for job in jobs.values():require(job['conclusion']=='success' and next(s['conclusion'] for s in job['steps'] if s['name'].startswith('Remove only this job'))=='success','collection or cleanup step failed')
    message={'collection_date_utc':DAY,'mode':'qualification','run_id':RUN,'run_attempt':'1','source_commit':SOURCE,'release_sha256':RELEASE}
    claim=load(directory/'claim-tag.json');require(claim['sha']==CLAIM and claim['object']['type']=='commit' and claim['object']['sha']==SOURCE and json.loads(claim['message'])==message,'exact immutable third qualification claim differs')
    refs={r['ref']:r['object']['sha'] for r in load(directory/'qualification-claim-refs.json') if r['object']['type']=='tag'}
    require(refs=={'refs/tags/qualification-reserved-main-v2-2026-11-07':'96f47c96f80cb9989d1a91c2e099c3e1b34210a8','refs/tags/qualification-reserved-main-v2-2026-11-08':'09f162d6d38fe0e4f6361bf5bd49c75aac86feb0','refs/tags/qualification-reserved-main-v2-2026-11-13':CLAIM},'both failures and approved third claim must remain counted')
    summary=load(directory/'replay'/('attempt-summary-'+RUN+'.json'))
    require(summary['parent_run_id']==RUN and summary['analysis_workflow_run_id']==REPLAY and summary['source_commit']==SOURCE and summary['release_sha256']==RELEASE and summary['claim_sha']==CLAIM
        and summary['mode']=='qualification' and summary['day']==DAY and summary['main_estimate'] is None and summary['qualification_included_in_main'] is False
        and summary['all_required_application_audits_passed'] is summary['four_distinct_research_hosts_verified'] is True,'automatic qualification replay or exclusion differs')
    roles=[];applications=[]
    with tempfile.TemporaryDirectory(prefix='qrl-amended-qualified-source-') as temporary:
        source=Path(temporary)/'source';subprocess.run(['git','clone','-q',str((directory/'public-source.bundle').resolve()),str(source)],check=True,capture_output=True)
        def git(*args):return subprocess.run(['git',*args],cwd=source,check=True,capture_output=True).stdout
        require(git('rev-list','--max-parents=0',SOURCE).decode().splitlines()==['b2c396b2b769b29308bc16b80cbc26de18b57d65'],'isolated public history required')
        raw=git('show',SOURCE+':release/release.json');require(sha(raw)==RELEASE,'frozen actual release differs');envelope=json.loads(raw)
        require(envelope['model_source_commit']==MODEL_SOURCE and len(envelope['source_files'])==155,'actual prepared source identity differs')
        for name,digest_value in envelope['source_files'].items():
            raw=git('show',SOURCE+':'+name);require(sha(raw)==digest_value==sha(git('show',MODEL_SOURCE+':'+name)),'measured source changed')
            if name.startswith(('experiments/','scripts/audit_main_evaluation.py')) and (ROOT/name).exists():require(sha((ROOT/name).read_bytes())==digest_value,'independent replay dependency differs')
        artifacts={}
        for name,row in envelope['artifacts'].items():
            raw=git('show',SOURCE+':'+row['path']);require(len(raw)==row['bytes'] and sha(raw)==row['sha256'],'actual seven-artifact descriptor differs');artifacts[name]=json.loads(raw)
        sample=artifacts['sample_plan'];require(sample['qualification_attempt_limit']==3 and sample['qualification_dates_utc']==[DAY] and sample['day_count']==20 and sample['collection_dates_utc'][0]=='2026-11-15' and sample['collection_dates_utc'][-1]=='2026-12-04','approved history-preserving protocol differs')
        permission=SimpleNamespace(artifacts=artifacts,_source_sha=envelope['source_files'],expected_sha256=RELEASE)
        for task in ('inventory','analytics'):
            print(json.dumps({'task':task,'stage':'extract_and_independent_replay'}),flush=True)
            with tempfile.TemporaryDirectory(prefix='qrl-amended-pair-') as pair_temp:
                pair=Path(pair_temp);paths={}
                for role in ('target','observer'):
                    stem=f'{DAY}-{task}-{role}-{RUN}-1';path=pair/role;manifest=extract(directory/'raw'/(stem+'.tar.gz'),directory/'raw'/(stem+'.json'),path);paths[role]=path/('main-'+task)
                    identity=load(path/'public-role-identity.json');report=load(paths[role]/'report.json');registration=load(path/'network-registration.json');cleanup=load(path/'network-cleanup.json');receipt=load(path/'registration-auth-key.json')
                    require(identity=={'task':task,'role':role,'day':DAY,'mode':'qualification','source_commit':SOURCE,'run_id':RUN,'run_attempt':'1','claim_sha':CLAIM,'release_sha256':RELEASE,'collection_step_outcome':'success'},'actual full role identity differs')
                    require(report['status']=='passed' and report['smoke'] is True and report['confirmatory_data'] is False and len(report['cases'])==len(report['cells'])==66
                        and {r['rows'] for r in report['cases']}=={1000,100000} and report['source_sha256']==envelope['source_files'],'full 66-cell two-scale panel differs')
                    require(report['authorization']['verified_calendar_claim']==message and report['authorization']['calendar_claim_sha']==CLAIM and report['authorization']['additional_spending_limit']==0,'actual panel authorization differs')
                    name=f'qrl-{RUN}{0 if task=="inventory" else 1}-1-{role}';verify_key_receipt(receipt,name)
                    require(registration['hostname']==name and registration['tag']==TAG and registration['verified_standard_node'] is True and registration['provider_is_ephemeral'] is not True
                        and registration['registration_auth_key_id']==receipt['key_id'] and registration['client_version']==VERSION and registration['client_tarball_sha256']==BINARY_SHA,'ordinary pinned registration differs')
                    require(cleanup=={'status':'removed','hostname':name,'node_id':registration['node_id'],'tag':TAG,'verified_absent':True},'exact own-node absence receipt differs')
                    require(report['local']['overlay_name']==name and report['local']['github_run_id']==RUN and report['local']['source_commit']==SOURCE,'actual runner source identity differs')
                    roles.append({'task':task,'role':role,'node_id':registration['node_id'],'kernel_boot_sha256':report['local']['kernel_boot_sha256'],'full_cells':66,'members_verified':len(manifest['files']),'exact_own_node_removed':True})
                commitment=load(paths['target']/'policy-commitment.json');offer=load(paths['observer']/'policy-commitment-offer.json');choices=load(paths['observer']/'daily-choices.json');profiles=load(paths['observer']/'daily-profiles.json')
                require(commitment==load(paths['observer']/'policy-commitment.json') and commitment['choices_sha256']==digest(choices) and commitment['profiles_sha256']==digest(profiles)
                    and offer['choices_sha256']==commitment['choices_sha256'] and offer['profiles_sha256']==commitment['profiles_sha256'] and commitment['release_sha256']==RELEASE and commitment['collection_date_utc']==DAY
                    and commitment['completed_cases']==18 and commitment['loaded_cases_started']==offer['loaded_cases_started']==0 and choices['loaded_evaluation_outcomes_used'] is profiles['loaded_evaluation_outcomes_used'] is False,'both original preloaded policy commitments required')
                require(0<int(commitment['target_monotonic_ns'])<int(load(paths['target']/'l-000/windows.json')[0]['start_monotonic_ns'])
                    and 0<int(offer['observer_monotonic_ns'])<int(load(paths['observer']/'l-000/loaded/loaded-start.json')['measurement_start_monotonic_ns']),'policy commitments postdate loaded start')
                replay_path=pair/'automatic';stem='application-replay-'+RUN+'-'+task;extract(directory/'replay'/(stem+'.tar.gz'),directory/'replay'/(stem+'.json'),replay_path)
                automatic=load(replay_path/'application-replay.json');retained=load(replay_path/'paired-audit.json')
                require(automatic==next(r['replay'] for r in summary['applications'] if r['task']==task) and automatic['status']=='passed'
                    and automatic['paired_auditor_sha256']==sha((ROOT/'scripts/audit_main_evaluation.py').read_bytes()) and not (replay_path/'component.json').exists(),'automatic paired auditor or qualification exclusion differs')
                save(output/(task+'-automatic-paired-audit.json'),retained)
                from scripts.audit_main_evaluation import audit as paired_audit
                try:
                    replayed=paired_audit(paths['target'],paths['observer'],permission)
                    require(replayed==retained,'fresh paired replay differs from actual Linux report');save(output/(task+'-independent-paired-audit.json'),replayed)
                    applications.append({'task':task,'status':'passed','cells':66,'policy_commitment_verified':True,'exact_paired_replay':True})
                except ValueError as error:
                    if str(error)!='policy choice replay differs':raise
                    trace=error.__traceback__
                    while trace and not (trace.tb_frame.f_code.co_name=='audit' and Path(trace.tb_frame.f_code.co_filename).resolve()==(ROOT/'scripts/audit_main_evaluation.py').resolve()):trace=trace.tb_next
                    require(trace is not None and len(trace.tb_frame.f_locals['summaries'])==66,'unexpected exact replay failure')
                    current=trace.tb_frame.f_locals;differences=[]
                    def compare(a,b,path=''):
                        if isinstance(a,dict) and isinstance(b,dict) and a.keys()==b.keys():
                            for k in a:compare(a[k],b[k],path+'/'+str(k))
                        elif isinstance(a,list) and isinstance(b,list) and len(a)==len(b):
                            for i,(x,y) in enumerate(zip(a,b)):compare(x,y,path+'/'+str(i))
                        elif a!=b:differences.append({'path':path,'retained':a,'replayed':b})
                    compare(choices,current['choices'],'choices');compare(profiles,current['profiles'],'profiles')
                    save(output/(task+'-exact-replay-differences.json'),{'error':str(error),'all_semantic_cells_traversed':66,'exact_profiles_equal':profiles==current['profiles'],'selected_actions_equal':choices['choices']==current['choices']['choices'],'differences':differences})
                    applications.append({'task':task,'status':'exact_replay_unresolved_on_this_platform','cells':66,'policy_commitment_verified':True,'exact_paired_replay':False,'score_differences':len(differences)})
                print(json.dumps(applications[-1]),flush=True)
    require(len({r['node_id'] for r in roles})==len({r['kernel_boot_sha256'] for r in roles})==4,'four distinct nodes and kernels required')
    passed=all(r['status']=='passed' for r in applications)
    result={'version':'independent-amended-full-qualification-v1','status':'passed' if passed else 'exact_linux_recheck_required','run_id':RUN,'source_commit':SOURCE,'release_sha256':RELEASE,
        'claim_sha':CLAIM,'provider_assets_verified':asset_count,'applications':applications,'roles':roles,'own_nodes_verified_removed':4,'qualification_passed':passed,
        'main_component_produced':False,'qualification_excluded_from_main':True,'refit_performed':False,'acquisition_started':False,'auditor_sha256':sha(Path(__file__).read_bytes())}
    save(output/'independent-qualification-audit.json',result);return result


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('directory',type=Path);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    print(json.dumps(audit(args.directory,args.output)))


if __name__=='__main__':main()
