"""Offline diagnosis of original final-qualification evidence; never a new attempt.

The frozen files remain unchanged. Explicit in-memory fixes are recorded beside
their original failures. This cannot pass the consumed qualification or produce
a main component, and does not fit or refit any model.
"""
import argparse
import copy
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from experiments.study_design.public_artifacts import extract
from scripts.audit_final_qualification_failure import RUN,SOURCE,RELEASE,audit

def load(path):return json.loads(gzip.decompress(path.read_bytes()) if path.suffix=='.gz' else path.read_bytes())
def sha(raw):return hashlib.sha256(raw).hexdigest()
def save(path,value):path.write_text(json.dumps(value,indent=2)+'\n')
def expect_failure(call,kind,message):
    try:call()
    except Exception as error:
        assert type(error).__name__==kind and str(error)==message,(type(error).__name__,str(error))
        return {'type':kind,'message':message,'reproduced':True}
    raise AssertionError('expected original failure was not reproduced')


def diagnose(directory,output):
    retained=audit(directory);output.mkdir(exist_ok=False)
    with tempfile.TemporaryDirectory(prefix='qrl-offline-final-diagnosis-') as temporary:
        temp=Path(temporary);source=temp/'source';subprocess.run(['git','clone','-q',str((directory/'public-source.bundle').resolve()),str(source)],check=True,capture_output=True)
        subprocess.run(['git','checkout','-q','--detach',SOURCE],cwd=source,check=True,capture_output=True)
        assert subprocess.check_output(['git','rev-parse','HEAD'],cwd=source,text=True).strip()==SOURCE
        envelope=load(source/'release/release.json');assert sha((source/'release/release.json').read_bytes())==RELEASE
        artifacts={k:load(source/row['path']) for k,row in envelope['artifacts'].items()}
        # Only parsed, already byte-bound artifacts are supplied to the offline
        # auditor. PerformancePermission would refit; this diagnosis does not.
        permission=SimpleNamespace(artifacts=artifacts,_source_sha=envelope['source_files'],expected_sha256=RELEASE)
        def paths(task):
            result={}
            for role in ('target','observer'):
                stem=f'2026-11-08-{task}-{role}-{RUN}-1';path=temp/(task+'-'+role)
                extract(directory/'raw'/(stem+'.tar.gz'),directory/'raw'/(stem+'.json'),path);result[role]=path/('main-'+task)
            return result
        inventory=paths('inventory');file=source/'scripts/audit_main_evaluation.py';original=file.read_text()
        def module(code,name,path):
            namespace={'__name__':name,'__file__':str(path),'__package__':'experiments.heldout_tasks' if 'performance_evidence' in name else None}
            exec(compile(code,str(path),'exec'),namespace);return namespace
        before=module(original,'qrl_original_main_auditor',file)
        inventory_failure=expect_failure(lambda:before['audit'](inventory['target'],inventory['observer'],permission),'TypeError',"'dict' object is not callable")
        old="for event,identity in zip(connections,identities):require(all(event[k]==identity[k] for k in ('connection_id','response_index','path','operation_id')),'gateway promoted identity differs')"
        new="for event,gateway_identity in zip(connections,identities):require(all(event[k]==gateway_identity[k] for k in ('connection_id','response_index','path','operation_id')),'gateway promoted identity differs')"
        assert original.count(old)==1;corrected=original.replace(old,new)
        (output/'inventory-auditor-variable-fix.py').write_text(corrected)
        after=module(corrected,'qrl_diagnostic_main_auditor',file)
        inventory_full_pass=False;inventory_followup=None
        try:
            result=after['audit'](inventory['target'],inventory['observer'],permission)
            assert result['status']=='passed' and len(result['cells'])==66 and result['smoke'] is True
            inventory_full_pass=True
            result['diagnostic_executable_sha256']=sha(corrected.encode());result['original_frozen_auditor_sha256']=sha(original.encode())
            result['role']='offline corrected-auditor diagnostic on qualification evidence; not a qualified release or main component'
            save(output/'inventory-paired-diagnostic.json',result)
        except ValueError as error:
            # Preserve a later exact policy/profile mismatch as unresolved. It
            # is never suppressed to claim a passing full paired replay.
            assert str(error)=='policy choice replay differs',str(error)
            trace=error.__traceback__
            while trace.tb_next:trace=trace.tb_next
            trace=error.__traceback__
            while trace and trace.tb_frame.f_code.co_name!='audit':trace=trace.tb_next
            assert trace is not None;local=trace.tb_frame.f_locals;assert len(local['summaries'])==66
            expected_profiles=load(inventory['observer']/'daily-profiles.json');expected_choices=load(inventory['observer']/'daily-choices.json')
            differences=[]
            def compare(left,right,path):
                if type(left) is not type(right):differences.append({'path':path,'retained':left,'replayed':right});return
                if isinstance(left,dict):
                    if set(left)!=set(right):differences.append({'path':path,'retained_keys':sorted(left),'replayed_keys':sorted(right)});return
                    for key in left:compare(left[key],right[key],path+'/'+key)
                elif isinstance(left,list):
                    if len(left)!=len(right):differences.append({'path':path,'retained_length':len(left),'replayed_length':len(right)});return
                    for i,(a,b) in enumerate(zip(left,right)):compare(a,b,path+'/'+str(i))
                elif left!=right:differences.append({'path':path,'retained':left,'replayed':right})
            compare(expected_profiles,local['profiles'],'profiles');compare(expected_choices,local['choices'],'choices')
            inventory_followup={'error_type':'ValueError','error':str(error),'full_paired_replay_passed':False,'cells_before_mismatch':66,
                'exact_profiles_equal':expected_profiles==local['profiles'],'exact_choices_equal':expected_choices==local['choices'],
                'chosen_actions_equal':expected_choices['choices']==local['choices']['choices'],'differences':differences}
            save(output/'inventory-exact-replay-difference.json',inventory_followup)
        analytics=paths('analytics');td=analytics['target']/'l-002';od=analytics['observer']/'l-002';packet=load(td/'profile-packet.json');case=packet['case']
        report=load(analytics['observer']/'report.json');generator_cpu=sorted(report['local']['allowed_cpus'])[0]
        warm=[{'id':i,'kind':'read'} for i in range(4)]
        bundles=[{'bundle':load(od/'warmup.json.gz'),'plan':warm,'prefix':'l-002-warm','cpu':generator_cpu,'serial':True},
            {'bundle':load(od/'operations.json.gz'),'plan':load(od/'schedule.json'),'prefix':'l-002-main','cpu':generator_cpu,'serial':False}]
        browser={'report':load(od/'loaded/loaded-browser.json'),'config':load(od/'loaded/loaded-browser-config.json')}
        file=source/'experiments/heldout_tasks/performance_evidence.py';original=file.read_text();before=module(original,'experiments.heldout_tasks.qrl_original_performance_evidence',file)
        analytics_failure=expect_failure(lambda:before['audit_cell'](case,packet,bundles,browser),'KeyError',"'text'")
        old="v['identifier']==identity and v['text']==label";new="v['identifier']==identity and v.get('text')==label"
        assert original.count(old)==1;corrected=original.replace(old,new)
        (output/'analytics-auditor-paint-text-fix.py').write_text(corrected)
        after=module(corrected,'experiments.heldout_tasks.qrl_diagnostic_performance_evidence',file)
        result=after['audit_cell'](case,packet,bundles,browser)
        assert result['browser']['eligible'] is True and len(result['browser']['probes'])==3
        save(output/'analytics-l-002-cell-diagnostic.json',result)
        bad=copy.deepcopy(browser)
        for row in bad['report']['probes']:
            for entry in row['paint_entries']:
                if entry['identifier']=='task-count' and 'text' in entry:entry['text']='wrong retained content'
        rejection=expect_failure(lambda:after['audit_cell'](case,packet,bundles,bad),'ValueError','content-matched visible paint missing')
        summary={'version':'final-qualification-offline-diagnosis-v1','status':'passed','run_id':RUN,'source_commit':SOURCE,'release_sha256':RELEASE,
            'original_inventory_replay_failure':inventory_failure,'inventory_full_66_cell_diagnostic_passed':inventory_full_pass,'inventory_followup':inventory_followup,
            'inventory_diagnostic_executable_sha256':sha((output/'inventory-auditor-variable-fix.py').read_bytes()),
            'original_analytics_cell_failure':analytics_failure,'analytics_diagnostic_cell':'l-002','analytics_cell_diagnostic_passed':True,'analytics_full_panel_completed':False,
            'analytics_diagnostic_executable_sha256':sha((output/'analytics-auditor-paint-text-fix.py').read_bytes()),'wrong_paint_text_rejected':rejection,
            'qualification_passed':False,'main_component_produced':False,'refit_performed':False,'acquisition_started':False,'qualification_attempt_budget_reset':False,
            'evidence_integrity_auditor_sha256':retained['auditor_sha256'],'diagnostic_driver_sha256':sha(Path(__file__).read_bytes())}
        save(output/'diagnosis.json',summary);return summary


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('directory',type=Path);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    print(json.dumps(diagnose(args.directory,args.output)))


if __name__=='__main__':main()
