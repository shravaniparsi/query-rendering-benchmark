"""Guarded file-backed reserved-task correctness suite."""
import argparse
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]; sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument('--output', type=Path, required=True); args = parser.parse_args()
    suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'), pattern='test_reserved_*.py')
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    value = {'role': 'reserved SQL/application correctness', 'run': result.testsRun, 'skipped': len(result.skipped),
        'errors': len(result.errors), 'failures': len(result.failures), 'comparative_timing': False,
        'status': 'passed' if result.wasSuccessful() and not result.skipped else 'failed'}
    args.output.parent.mkdir(parents=True, exist_ok=True); args.output.write_text(json.dumps(value, indent=2) + '\n')
    if value['status'] != 'passed': raise RuntimeError('Reserved correctness contracts incomplete')


if __name__ == '__main__': main()
