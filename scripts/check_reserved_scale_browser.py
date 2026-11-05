"""Replay scaled SQL/browser workflows with whole-table checks, without timing estimates."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]; sys.path.insert(0, str(ROOT))
from experiments.heldout_tasks.runtime import reserved_runtime
from experiments.heldout_tasks.scale_reference import InventoryScaleReference, AnalyticsScaleReference
from experiments.heldout_tasks.inventory import require


def main():
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--rows', type=int, choices=(1000,100000), required=True)
    parser.add_argument('--paint-contract', action='store_true', help='Verify content-matched visible paint markers without emitting times')
    parser.add_argument('--node', default=shutil.which('node')); parser.add_argument('--chrome'); parser.add_argument('--node-modules')
    args = parser.parse_args(); dsn = os.environ['QRL_TEST_DATABASE_URL']; output = args.output.resolve(); output.mkdir(parents=True, exist_ok=False)
    report = {'role': 'reserved scale SQL/browser correctness', 'fixture_rows': args.rows, 'status': 'started', 'comparative_timing': False, 'selector_fit': False,
        'source_commit': os.environ.get('GITHUB_SHA'), 'cases': []}
    try:
        for task in ('inventory', 'analytics'):
            for kind in ('correlated', 'batched', 'maintained'):
                folder = output / (task + '-' + kind); folder.mkdir(); reports = []
                with reserved_runtime(dsn, folder, task, kind, scale_rows=args.rows) as runtime:
                    for failure in (False, True):
                        if failure: runtime['origin'].command('fault', 503)
                        directory = folder / ('failure' if failure else 'normal'); directory.mkdir()
                        config = {'app_url': runtime['url'], 'task': task, 'query_kind': kind, 'failure': failure, 'paint_contract': args.paint_contract,
                            'prefix': folder.name + ('-failure' if failure else '-normal'), 'correctness_timeout_ms': 120000, 'directory': str(directory), 'report_path': str(directory / 'browser.json')}
                        config_path = directory / 'config.json'; config_path.write_text(json.dumps(config, indent=2) + '\n')
                        env = dict(os.environ)
                        if args.chrome: env['QRL_CHROME'] = args.chrome
                        if args.node_modules: env['QRL_NODE_MODULES'] = args.node_modules
                        with (directory / 'node.log').open('w') as log:
                            subprocess.run([args.node, str(ROOT / 'experiments/heldout_tasks/browser_check.cjs'), str(config_path)], env=env, check=True, stdout=log, stderr=subprocess.STDOUT, timeout=360)
                        value = json.loads((directory / 'browser.json').read_text()); require(value['status'] == 'passed', 'browser incomplete'); reports.append(value)
                        if failure: runtime['origin'].command('fault', None)
                    runtime['quiesce'](); state = runtime['fixture'].validation_state(); reference = (InventoryScaleReference if task == 'inventory' else AnalyticsScaleReference)(state)
                    final = reference.verify_final(); (folder / 'validation-state.json').write_text(json.dumps(state, indent=2) + '\n')
                    operations = {row['operation_id' if task == 'inventory' else 'event_id']: row for row in state['operations']}
                    backend = [json.loads(line) for line in (folder / 'backend.jsonl').read_text().splitlines()]
                    by_id = {event['operation_id']: event for event in backend}; require(len(by_id) == len(backend), 'duplicate backend request identity')
                    required_backend = set()
                    snapshots = actions = failures = 0
                    for value in reports:
                        for probe in value['probes']:
                            require(probe['checks_passed'], 'browser checks incomplete')
                            if probe['strategy'] == 'ssr':
                                event = by_id[probe['operation_id']]; required_backend.add(probe['operation_id'])
                                require(event['path'] == '/query' and event['status'] == (503 if probe['expected_failure'] else 200), 'SSR backend identity/status differs')
                                if not probe['expected_failure']: require(event['snapshot'] == probe['evidence'][0]['snapshot'], 'SSR backend/DOM snapshot differs')
                            for response in probe['responses']:
                                if response['path'] not in ('/api/task', '/api/task/mutate'): continue
                                event = by_id[response['operation_id']]; required_backend.add(response['operation_id'])
                                require(event['path'] == ('/query' if response['path'] == '/api/task' else '/control/mutate'), 'API backend route differs')
                                require(event['status'] == response['status'] and event['snapshot'] == response['json'].get('snapshot'), 'API backend status/snapshot differs')
                                if response['path'] == '/api/task' and response['status'] == 200: reference.verify_query(response['json'])
                            if probe['expected_failure']: failures += 1; continue
                            require(probe['trusted_clicks'] == [{'id': 'primary-action', 'trusted': True}, {'id': 'secondary-action', 'trusted': True}], 'business actions were not trusted')
                            for evidence in probe['evidence']: reference.verify_query(evidence); snapshots += 1
                            for action in probe['actions']:
                                payload, result = action['request'], action['result']; key = payload['operation_id' if task == 'inventory' else 'event_id']; operation = operations[key]
                                expected = ({'product_id': None, 'quantity': None, **{k: v for k, v in payload.items() if k != 'operation_id'}} if task == 'inventory' else
                                    {'sale_id': None, 'region': None, 'category': None, **payload})
                                require(operation['request'] == expected and operation['result'] == {k: v for k, v in result.items() if k != 'ok'}, 'browser business action differs from committed ledger')
                                actions += 1
                    require(required_backend == set(by_id), 'unassigned backend requests')
                    report['cases'].append({'task': task, 'query_kind': kind, 'status': 'passed', 'snapshots': snapshots,
                        'trusted_actions': actions, 'visible_failures': failures, 'final_oracle': final})
                    print(json.dumps(report['cases'][-1]), flush=True)
        report['status'] = 'passed'; report['totals'] = {key: sum(row[key] for row in report['cases']) for key in ('snapshots', 'trusted_actions', 'visible_failures')}
    finally:
        paths = [*list((ROOT / 'experiments/heldout_tasks').glob('*.py')), *list((ROOT / 'experiments/heldout_tasks').glob('*.cjs')),
            *list((ROOT / 'apps/reserved-tasks').rglob('*')), Path(__file__)]
        report['source_sha256'] = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths) if p.is_file() and p.suffix in ('.py', '.cjs', '.js', '.html')}
        (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')


if __name__ == '__main__': main()
