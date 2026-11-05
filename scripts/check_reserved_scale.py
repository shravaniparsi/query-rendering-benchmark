"""Largest-fixture correctness and whole-table checks, without timing outputs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]; sys.path.insert(0, str(ROOT))
from experiments.heldout_tasks.scale_sql import ScaleInventorySQL, ScaleAnalyticsSQL
from experiments.heldout_tasks.scale_reference import InventoryScaleReference, AnalyticsScaleReference


def main():
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument('--rows', type=int, default=100000)
    parser.add_argument('--output', type=Path, required=True); args = parser.parse_args(); output = args.output.resolve(); output.mkdir(parents=True, exist_ok=False)
    report = {'role': 'reserved scale correctness only', 'comparative_timing': False, 'selector_fit': False, 'source_commit': os.environ.get('GITHUB_SHA'), 'cases': [], 'status': 'started'}
    try:
        for task, cls, reference_cls in (('inventory', ScaleInventorySQL, InventoryScaleReference), ('analytics', ScaleAnalyticsSQL, AnalyticsScaleReference)):
            fixture = cls(os.environ['QRL_TEST_DATABASE_URL'], 'qrl_reserved_' + uuid.uuid4().hex)
            try:
                fixture.initialize_scale(args.rows, 'maintained'); folder = output / task; folder.mkdir(); probes = []
                if task == 'inventory':
                    probes.append(fixture.query()); fixture.apply('large-reserve', 'reserve', 'large-reservation', product_id=1, quantity=1); probes.append(fixture.query())
                    fixture.apply('large-cancel', 'cancel', 'large-reservation'); fixture.apply('cancel-initial', 'cancel', 'seed-reservation-5'); probes.append(fixture.query())
                    probes.append(fixture.query(category='category-1', brand='brand-2', available_only=False, offset=2, limit=5))
                else:
                    probes.append(fixture.query(100, 3700)); fixture.append('large-refund', 'refund', 110, 100, sale_id='seed-sale-0'); probes.append(fixture.query(100, 200))
                    fixture.append('large-late', 'sale', 99, 200, region='late', category='late'); probes.append(fixture.query(99, 100))
                    probes.append(fixture.query(100, 700, group_by=('region',), region='region-1', limit=1, offset=1))
                state = fixture.validation_state(); reference = reference_cls(state); final = reference.verify_final()
                for probe in probes: reference.verify_query(probe)
                with fixture.connection() as connection: version = connection.execute('SELECT version()').fetchone()[0]
                (folder / 'validation-state.json').write_text(json.dumps(state, indent=2) + '\n'); (folder / 'queries.json').write_text(json.dumps(probes, indent=2) + '\n')
                report['cases'].append({'task': task, 'rows': args.rows, 'query_kind': 'maintained', 'status': 'passed', 'snapshots': len(probes), 'postgres': version, 'final_oracle': final})
                print(json.dumps(report['cases'][-1]), flush=True)
            finally: fixture.drop()
        report['status'] = 'passed'
    finally:
        paths = [ROOT / 'experiments/heldout_tasks/scale_sql.py', ROOT / 'experiments/heldout_tasks/scale_reference.py', Path(__file__)]
        report['source_sha256'] = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
        (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')


if __name__ == '__main__': main()
