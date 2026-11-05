"""Run finite-score, learner, strong-baseline and actual-artifact guard tests."""
import argparse
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]; sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument('--output', type=Path, required=True); args = parser.parse_args()
    patterns = ('test_study_design.py', 'test_feasibility_learner.py', 'test_policy_selection.py', 'test_artifact_release.py', 'test_development_analysis.py', 'test_main_selection.py')
    suite = unittest.TestSuite(unittest.defaultTestLoader.discover(str(ROOT / 'tests'), pattern=pattern) for pattern in patterns)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    value = {'role': 'decision software contracts; synthetic test fixtures only', 'status': 'passed' if result.wasSuccessful() and not result.skipped else 'failed',
        'run': result.testsRun, 'skipped': len(result.skipped), 'errors': len(result.errors), 'failures': len(result.failures), 'experimental_model_fitted': False}
    args.output.parent.mkdir(parents=True, exist_ok=True); args.output.write_text(json.dumps(value, indent=2) + '\n')
    if value['status'] != 'passed': raise RuntimeError('Decision software contracts incomplete')


if __name__ == '__main__': main()
