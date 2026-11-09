"""Verify final v2 qualification failure evidence, without qualifying or scoring it.

This auditor accepts only the source-bound November 8 second attempt. It checks
retention, the actual failure signatures and exact own-node removal. It never
starts acquisition, fits a model, resets the attempt budget or enables main.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from experiments.study_design.decision import require
from experiments.study_design.public_artifacts import extract
from scripts.research_network import verify_key_receipt,TAG,VERSION,BINARY_SHA

RUN='42500110887';REPLAY='42503510768';DAY='2026-11-08'
SOURCE='f9d4f32f02115f18c14652eba447f28fa2a7fbdb'
RELEASE='33f3db86ce92d432de6299fbec1ab7d2f10df91b97fcf9008422c4c7d1597ca5'
CLAIM='09f162d6d38fe0e4f6361bf5bd49c75aac86feb0'
MODEL_SOURCE='aa52178f7067639055ba66761d184a97fe668593'
def sha(raw):return hashlib.sha256(raw).hexdigest()
def load(path):return json.loads(path.read_bytes())


def audit(directory):
    providers={};asset_count=0
    for folder,tag,count in [('raw','qualification-reserved-main-v2-run-'+RUN,8),('execution','execution-'+RUN,2),('replay','replay-'+RUN,5),('replay-execution','replay-execution-'+REPLAY,2)]:
        provider=load(directory/folder/'provider-release.json');providers[folder]=provider
        require(provider['tag_name']==tag and provider['target_commitish']==SOURCE and len(provider['assets'])==count,'complete original source-bound provider assets required')
        require(len({a['name'] for a in provider['assets']})==count,'duplicate provider asset')
        for asset in provider['assets']:
            require(Path(asset['name']).name==asset['name'],'confined provider asset required');raw=(directory/folder/asset['name']).read_bytes()
            require(len(raw)==asset['size'] and asset['digest']=='sha256:'+sha(raw),'provider asset digest differs');asset_count+=1
    metadata=load(directory/'execution'/('public-execution-'+RUN+'-1-metadata.json'))
    require(str(metadata['databaseId'])==RUN and metadata['headSha']==SOURCE and metadata['status']=='completed' and metadata['conclusion']=='failure'
        and metadata['event']=='schedule' and metadata['workflowName']=='Reserved Main Evaluation','actual completed failed qualification metadata differs')
    jobs={j['name']:j for j in metadata['jobs'] if j['name'].startswith('collection (')}
    require(len(jobs)==4,'four collection roles required')
    for task in ('inventory','analytics'):
        for role in ('target','observer'):
            job=jobs[f'collection ({task}, {role})'];outcome='success' if task=='inventory' else 'failure'
            require(job['conclusion']==outcome and next(s['conclusion'] for s in job['steps'] if s['name'].startswith('Remove only this job'))=='success','actual role or cleanup step outcome differs')
    log=(directory/'execution'/('public-execution-'+RUN+'-1-all-jobs.log')).read_text()
    require(all(f'collection ({task}, {role})' in log for task in ('inventory','analytics') for role in ('target','observer')) and "KeyError: 'text'" in log
        and 'RuntimeError: observer idle timeout' in log,'complete masked acquisition failure logs required')
    replay_log=(directory/'replay-execution'/('public-application-replay-'+REPLAY+'-1-all-jobs.log')).read_text()
    require("TypeError: 'dict' object is not callable" in replay_log and 'raw role identity or collection outcome differs' in replay_log,'original automatic replay failures missing')
    message={'collection_date_utc':DAY,'mode':'qualification','run_id':RUN,'run_attempt':'1','source_commit':SOURCE,'release_sha256':RELEASE}
    claim=load(directory/'claim-tag.json')
    require(claim['sha']==CLAIM and claim['object']=={'sha':SOURCE,'type':'commit','url':f'https://api.github.com/repos/shravaniparsi/query-rendering-benchmark/git/commits/{SOURCE}'}
        and json.loads(claim['message'])==message,'actual annotated second qualification claim differs')
    refs={r['ref']:r['object']['sha'] for r in load(directory/'qualification-claim-refs.json') if r['object']['type']=='tag'}
    require(refs=={'refs/tags/qualification-reserved-main-v2-2026-11-07':'96f47c96f80cb9989d1a91c2e099c3e1b34210a8','refs/tags/qualification-reserved-main-v2-2026-11-08':CLAIM},'original two consumed qualification dates required')
    gates={r['name']:r['value'] for r in load(directory/'gate-readback.json')}
    require(gates=={'QRL_MAIN_ENABLED':'false','QRL_QUALIFICATION_ENABLED':'false','QRL_FROZEN_EXECUTION_SHA':SOURCE,'QRL_RELEASE_SHA256':RELEASE},'acquisition must remain disabled at exhausted budget')
    roles=[]
    with tempfile.TemporaryDirectory(prefix='qrl-final-qualification-') as temporary:
        temp=Path(temporary);subprocess.run(['git','clone','-q',str((directory/'public-source.bundle').resolve()),str(temp/'source')],check=True,capture_output=True)
        def git(*args):return subprocess.run(['git',*args],cwd=temp/'source',check=True,capture_output=True).stdout
        require(git('rev-list','--max-parents=0',SOURCE).decode().splitlines()==['b2c396b2b769b29308bc16b80cbc26de18b57d65'],'unexpected private source history')
        raw=git('show',SOURCE+':release/release.json');require(sha(raw)==RELEASE,'actual frozen release differs');envelope=json.loads(raw)
        require(envelope['model_source_commit']==MODEL_SOURCE and len(envelope['source_files'])==149,'actual corrected measurement source differs')
        for name,digest in envelope['source_files'].items():require(sha(git('show',SOURCE+':'+name))==sha(git('show',MODEL_SOURCE+':'+name))==digest,'frozen source bytes differ')
        artifacts={}
        for name,row in envelope['artifacts'].items():
            raw=git('show',SOURCE+':'+row['path']);require(len(raw)==row['bytes'] and sha(raw)==row['sha256'],'actual artifact bytes differ');artifacts[name]=json.loads(raw)
        sample=artifacts['sample_plan'];require(sample['day_count']==20 and sample['collection_dates_utc'][0]=='2026-11-09' and sample['collection_dates_utc'][-1]=='2026-11-28'
            and sample['qualification_dates_utc']==['2026-11-07','2026-11-08'],'original unused main and consumed qualification calendar required')
        audited_source=git('show',SOURCE+':scripts/audit_main_evaluation.py').decode()
        require('for event,identity in zip(connections,identities)' in audited_source and "expected=identity(t['local']['run_id']" in audited_source,'retained independent replay name collision differs')
        for task in ('inventory','analytics'):
            for role in ('target','observer'):
                stem=f'{DAY}-{task}-{role}-{RUN}-1';path=temp/(task+'-'+role);manifest=extract(directory/'raw'/(stem+'.tar.gz'),directory/'raw'/(stem+'.json'),path)
                report=load(path/('main-'+task)/'report.json');identity=load(path/'public-role-identity.json');registration=load(path/'network-registration.json');cleanup=load(path/'network-cleanup.json');receipt=load(path/'registration-auth-key.json')
                outcome='success' if task=='inventory' else 'failure'
                require(identity=={'task':task,'role':role,'day':DAY,'mode':'qualification','source_commit':SOURCE,'run_id':RUN,'run_attempt':'1','claim_sha':CLAIM,'release_sha256':RELEASE,'collection_step_outcome':outcome}
                    and report['task']==task and report['collection_date_utc']==DAY and report['smoke'] is True and report['confirmatory_data'] is False
                    and report['status']==('passed' if task=='inventory' else 'failed'),'qualification identity or actual panel status differs')
                name=f'qrl-{RUN}{0 if task=="inventory" else 1}-1-{role}';local=report['local'];authorization=report['authorization']
                require(local['github_run_id']==RUN and local['source_commit']==SOURCE and local['overlay_name']==name and authorization['verified_calendar_claim']==message
                    and authorization['calendar_claim_sha']==CLAIM and authorization['additional_spending_limit']==0,'actual acquisition source/claim identity differs')
                verify_key_receipt(receipt,name)
                require(registration['hostname']==name and registration['tag']==TAG and registration['verified_standard_node'] is True and registration['registration_auth_key_id']==receipt['key_id']
                    and registration['client_version']==VERSION and registration['client_tarball_sha256']==BINARY_SHA and registration['provider_is_ephemeral'] is not True,'ordinary pinned registration differs')
                require(cleanup=={'status':'removed','hostname':name,'node_id':registration['node_id'],'tag':TAG,'verified_absent':True},'exact own-node deletion/absence record differs')
                require(len(report['cases'])==66 and {c['rows'] for c in report['cases']}=={1000,100000} and report['source_sha256']==envelope['source_files'],'full two-scale declared panel/source required')
                count=66 if task=='inventory' else (21 if role=='target' else 20)
                require(len(report['cells'])==count and all(all(cell[k]==v for k,v in case.items()) for cell,case in zip(report['cells'],report['cases'])),'retained actual completed prefix differs')
                if task=='analytics' and role=='target':require(report['error_type']=='RuntimeError' and report['error']=='observer idle timeout','bounded target failure differs')
                if task=='analytics' and role=='observer':
                    require(report['error_type']=='KeyError' and report['error']=="'text'",'actual analytics paint-auditor failure differs')
                    browser=load(path/'main-analytics/l-002/loaded/loaded-browser.json');probe=browser['probes'][0];entries=probe['paint_entries']
                    require(all(p['outcome']=='completed' and p['integrity_error'] is False for p in browser['probes']) and entries[0]['identifier']=='task-count' and 'text' not in entries[0],
                        'original completed probe and incomplete earlier paint record required')
                    text=probe['evidence'][0]['visible_text'];labels={'task-count':text['count'],'task-first-title':text['titles'][0],'task-first-value':text['values'][0]}
                    matches=[next(p for p in entries if p['identifier']==key and p.get('text')==value) for key,value in labels.items()]
                    require(all(p['renderTime']>entries[0]['renderTime'] and p['rect']['width']>0 and p['rect']['height']>0 for p in matches)
                        and max(p['renderTime'] for p in matches)==probe['critical_content_paint_ms'],'later content-matched paint evidence differs')
                roles.append({'task':task,'role':role,'node_id':registration['node_id'],'kernel_boot_sha256':local['kernel_boot_sha256'],'cleanup_verified':True,
                    'complete_cells':count,'status':report['status'],'error_type':report.get('error_type'),'error':report.get('error'),'retained_members':len(manifest['files'])})
        require(len({r['node_id'] for r in roles})==len({r['kernel_boot_sha256'] for r in roles})==4,'four distinct original runner/node identities required')
        summary=load(directory/'replay'/('attempt-summary-'+RUN+'.json'))
        require(summary['parent_run_id']==RUN and summary['analysis_workflow_run_id']==REPLAY and summary['source_commit']==SOURCE and summary['release_sha256']==RELEASE
            and summary['claim_sha']==CLAIM and summary['day']==DAY and summary['mode']=='qualification' and summary['main_estimate'] is None
            and summary['qualification_included_in_main'] is False and summary['all_required_application_audits_passed'] is False,'failed qualification entered a main estimate')
        for task in ('inventory','analytics'):
            stem='application-replay-'+RUN+'-'+task;path=temp/('replay-'+task)
            manifest=extract(directory/'replay'/(stem+'.tar.gz'),directory/'replay'/(stem+'.json'),path);result=load(path/'application-replay.json')
            require(len(manifest['files'])==1 and result['status']=='unavailable' and result['task']==task and result['run_id']==RUN and result['source_commit']==SOURCE
                and result['release_sha256']==RELEASE and result['mode']=='qualification' and result['claim_sha']==CLAIM
                and next(r for r in summary['applications'] if r['task']==task)['replay']==result,'original unavailable automatic replay differs')
            expected=('TypeError',"'dict' object is not callable") if task=='inventory' else ('ValueError','raw role identity or collection outcome differs')
            require((result['error_type'],result['error'])==expected,'actual automatic replay rejection differs')
    return {'version':'final-full-compact-qualification-failure-v1','status':'passed','role':'evidence integrity and own-node cleanup only','qualification_passed':False,'main_score_produced':False,
        'run_id':RUN,'source_commit':SOURCE,'release_sha256':RELEASE,'provider_assets_verified':asset_count,'own_nodes_verified_removed':4,'roles':roles,
        'consumed_v2_qualification_dates':['2026-11-07','2026-11-08'],'qualification_budget_exhausted':True,'both_acquisition_flags_disabled':True,
        'original_automatic_replay_both_applications_unavailable':True,'qualification_excluded_from_main':True,'auditor_sha256':sha(Path(__file__).read_bytes())}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('directory',type=Path);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    value=audit(args.directory);args.output.write_text(json.dumps(value,indent=2)+'\n');print(json.dumps(value))


if __name__=='__main__':main()
