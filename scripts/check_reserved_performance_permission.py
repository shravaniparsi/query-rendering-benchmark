"""Check actual release/date/configuration bytes before a browser can launch."""
import argparse
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from experiments.study_design.performance_release import PerformancePermission


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('config',type=Path);args=parser.parse_args();value=json.loads(args.config.read_bytes());identity=value['permission'];case=value['case']
    permission=PerformancePermission(identity['root'],identity['release_path'],identity['expected_sha256'])
    result=permission.permit(case['task'],case['phase'],case['rows'],case['candidate'],case['strategy'],case['collection_date_utc'],context_id=case.get('context_id'))
    print(json.dumps({key:result[key] for key in ('status','application','phase','collection_date_utc','release_sha256')}))
if __name__=='__main__':main()
