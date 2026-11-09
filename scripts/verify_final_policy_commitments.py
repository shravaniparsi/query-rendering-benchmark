"""Check the failed final qualification's original preloaded policy commitments."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import tarfile

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts.audit_final_qualification_failure import audit,RUN,DAY,RELEASE
from experiments.heldout_tasks.performance_evidence import digest


def verify(directory):
    retained=audit(directory);results=[]
    for task in ('inventory','analytics'):
        with tarfile.open(directory/'raw'/f'{DAY}-{task}-target-{RUN}-1.tar.gz') as target,tarfile.open(directory/'raw'/f'{DAY}-{task}-observer-{RUN}-1.tar.gz') as observer:
            def load(archive,name):return json.loads(archive.extractfile('main-'+task+'/'+name).read())
            commitment=load(target,'policy-commitment.json');other=load(observer,'policy-commitment.json');offer=load(observer,'policy-commitment-offer.json')
            choices=load(observer,'daily-choices.json');profiles=load(observer,'daily-profiles.json')
            assert commitment==other and commitment['release_sha256']==RELEASE and commitment['collection_date_utc']==DAY
            assert commitment['choices_sha256']==digest(choices) and commitment['profiles_sha256']==digest(profiles)
            assert offer['choices_sha256']==commitment['choices_sha256'] and offer['profiles_sha256']==commitment['profiles_sha256']
            assert commitment['completed_cases']==18 and commitment['loaded_cases_started']==offer['loaded_cases_started']==0
            assert choices['loaded_evaluation_outcomes_used'] is False and profiles['loaded_evaluation_outcomes_used'] is False
            assert set(choices['choices'])=={'joint','query_only','profile_fixed'}
            for selected in choices['choices'].values():assert len(selected)==8 and all(len(v)==27 for v in selected.values())
            target_start=int(load(target,'l-000/windows.json')[0]['start_monotonic_ns'])
            observer_start=int(load(observer,'l-000/loaded/loaded-start.json')['measurement_start_monotonic_ns'])
            assert 0<int(commitment['target_monotonic_ns'])<target_start and 0<int(offer['observer_monotonic_ns'])<observer_start
            results.append({'task':task,'status':'passed','choices_sha256':digest(choices),'profiles_sha256':digest(profiles),'completed_serial_and_overhead_cases':18,
                'loaded_cases_started_at_commitment':0,'context_count':8,'scenarios_per_context':27,'loaded_outcomes_used':False,
                'target_commitment_precedes_loaded_start':True,'observer_offer_precedes_loaded_start':True})
    return {'version':'failed-final-qualification-policy-commitments-v1','status':'passed','run_id':RUN,'release_sha256':RELEASE,'applications':results,
        'qualification_passed':False,'main_component_produced':False,'evidence_auditor_sha256':retained['auditor_sha256'],
        'verifier_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('directory',type=Path);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    result=verify(args.directory);args.output.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))


if __name__=='__main__':main()
