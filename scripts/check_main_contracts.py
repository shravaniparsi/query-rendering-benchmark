"""Run pre-acquisition calendar, profile, clock, fallback and permission tests."""
import argparse
import json
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    patterns=('test_main_design.py','test_main_evidence.py','test_performance_permission.py','test_main_selection.py','test_calendar_analysis.py','test_public_execution.py','test_release_preparation.py')
    suite=unittest.TestSuite(unittest.defaultTestLoader.discover(str(ROOT/'tests'),pattern=p) for p in patterns);result=unittest.TextTestRunner(verbosity=2).run(suite)
    value={'role':'pre-acquisition main software contracts; synthetic fixtures only','status':'passed' if result.wasSuccessful() and not result.skipped else 'failed',
        'run':result.testsRun,'skipped':len(result.skipped),'errors':len(result.errors),'failures':len(result.failures),'reserved_performance_acquired':False}
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(value,indent=2)+'\n')
    if value['status']!='passed':raise RuntimeError('Main software contracts incomplete')


if __name__=='__main__':main()
