"""Verify the loaded browser's local monotonic clock before collecting probes."""
import argparse
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from experiments.issue_dashboard.loaded_browser import check_clock
if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();print(check_clock(args.output)['bracket_width_ms'])
