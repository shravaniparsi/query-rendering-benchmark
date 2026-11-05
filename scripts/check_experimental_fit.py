"""Replay the exact actual development fit on the declared analysis platform."""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from experiments.study_design.decision import require
from experiments.study_design.learner import fit,canonical_sha


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for key in ('data','fit','output'):parser.add_argument('--'+key,type=Path,required=True)
    args=parser.parse_args();raw=(args.data/'training-inputs.json').read_bytes();training=json.loads(raw);model_raw=(args.fit/'model.json').read_bytes();model=json.loads(model_raw)
    registry=json.loads((ROOT/'paper/followup/query-rendering-load/protocol/application_reservation_v1.json').read_bytes())
    reproduced=fit(training['rows'],registry,training_inputs_sha256=hashlib.sha256(raw).hexdigest())
    require(canonical_sha(model)==canonical_sha(reproduced),'actual fitted model is not exactly reproducible')
    report={'status':'passed','role':'exact actual development-model replay; no reserved observations',
        'training_inputs_sha256':hashlib.sha256(raw).hexdigest(),'model_file_sha256':hashlib.sha256(model_raw).hexdigest(),
        'canonical_model_sha256':canonical_sha(model),'training_rows':len(training['rows']),'fit_applications':model['fit_applications'],
        'python':sys.version,'platform':platform.platform(),'replayer_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    args.output.write_text(json.dumps(report,indent=2)+'\n');print(json.dumps({'status':report['status'],'training_rows':report['training_rows']}))
if __name__=='__main__':main()
