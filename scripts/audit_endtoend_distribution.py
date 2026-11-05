"""Independent replay of ranked two-VM development evidence; no fitted decisions."""
import argparse
from collections import Counter, defaultdict
import gzip
import hashlib
import json
import math
from pathlib import Path
import re
import statistics
import subprocess
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]; sys.path.insert(0, str(ROOT))
from experiments.issue_dashboard.arrivals import schedule
from experiments.issue_dashboard.distribution import DistributionOracle
from experiments.issue_dashboard.endtoend_design import design, cases, trace, qualification_order, ROLE, SEED
from experiments.issue_dashboard.endtoend_transport import POLICY
from experiments.issue_dashboard.offhost_engineering import require, verify_routes
from experiments.issue_dashboard.offhost_loaded_metrics import check_clock, check_loaded
from experiments.issue_dashboard.cpu_budget import verify_browser_sample
from experiments.issue_dashboard.path_controls import identity, body_for
from experiments.issue_dashboard.wal_observer import delta
from experiments.issue_dashboard.comparison_metrics import verify_backend_coverage, backend_summaries
from experiments.query_rendering_load.calibrate import summarize
from scripts.audit_offhost_resources import check_budget, check_browser, usage


def load(path):
    return json.loads(gzip.decompress(path.read_bytes()) if path.suffix == '.gz' else path.read_bytes())


class RankedOracle(DistributionOracle):
    verify = DistributionOracle.verify_ranked


def server_connections(events, target_cpu):
    groups = defaultdict(list); by_request = defaultdict(list)
    for event in events:
        require(re.fullmatch('[0-9a-f]{32}', event['connection_id']) and event['http_version'] == 'HTTP/1.1' and
            event['tcp_nodelay_raw'] == 1 and event['allowed_cpus'] == [target_cpu], 'server socket identity/CPU/HTTP differs')
        require(event['outcome'] in ('sent', 'failed') and type(event['body_bytes']) is int and event['body_bytes'] >= 0 and
            re.fullmatch('[0-9a-f]{64}', event['body_sha256']), 'server response evidence differs')
        groups[event['connection_id']].append(event)
        by_request[event['operation_id'], event['method'], event['path']].append(event)
    for values in groups.values():
        require([e['response_index'] for e in values] == list(range(1, len(values) + 1)), 'server response order differs')
        require(len({tuple(e['client_address']) + tuple(e['server_address']) for e in values}) == 1, 'connection tuple changed')
    return by_request


def reconcile(native, server, cpu, *, browser=False):
    if browser:
        key = native['operation_id'], 'GET', native['path']; cid = native['connection_id']; completed = True
    else:
        require(native['policy'] == POLICY and native['attempts'] == 1 and native['persistent'] is True and
            native['allowed_cpus'] == [cpu] and native['outcome'] in ('completed', 'failed'), 'native ownership/retry/CPU differs')
        require(int(native['validation_finish_ns']) > 0, 'receipt validation clock missing')
        if native['body_receipt_ns'] is not None:
            require(0 < int(native['body_receipt_ns']) <= int(native['validation_finish_ns']), 'receipt clock follows validation')
        key = native['operation_id'], native['method'], urlsplit(native['path']).path
        cid = native['server_connection_id']; completed = native['outcome'] == 'completed'
    if completed:
        require(browser or native['http_version'] == 11, 'client HTTP version differs')
        matches = [e for e in server[key] if e['connection_id'] == cid]
        require(len(matches) == 1, 'native receipt lacks unique gateway response')
        event = matches[0]
        require(all(native[k] == event[k] for k in ('status', 'body_bytes', 'body_sha256')), 'native/gateway body or status differs')
        if not browser:
            require(native['client_address'] == event['client_address'] and native['peer_address'] == event['server_address'], 'socket endpoint differs')
    return key


def bundle(value, prefix, cpu, server, oracle, required, allowed, *, plan=None, serial=False, closed=False):
    rows = value['operations']; phase = value['phase']; sessions = value['sessions']; native = defaultdict(list)
    require(phase['workers_joined'] is True and phase['attempts_per_http_request'] == 1 and
        0 < int(phase['start_monotonic_ns']) <= int(phase['finish_monotonic_ns']), 'worker/phase clock differs')
    require(len({s['worker'] for s in sessions}) == len(sessions) <= phase['workers'], 'worker identities differ')
    owners = {}; connection_owner = {}
    for session in sessions:
        owners[session['worker']] = session['owner_thread']; counts = Counter()
        for receipt in session['records']:
            reconcile(receipt, server, cpu); native[receipt['operation_id']].append(receipt)
            cid = receipt['server_connection_id']
            if cid:
                require(cid not in connection_owner or connection_owner[cid] == session['worker'], 'connection shared across worker owners')
                connection_owner[cid] = session['worker']; counts[cid] += 1
        require(all(n <= 32 for n in counts.values()), 'owned connection exceeds request cap')
    require(len(set(owners.values())) == len(owners), 'workers share owner thread')
    require([r['id'] for r in rows] == list(range(len(rows))), 'logical identities/coverage differ')
    if plan is not None: require(len(rows) == len(plan) and all(all(r.get(k) == v for k, v in p.items()) for r, p in zip(rows, plan)), 'offered trace differs')
    if closed:
        require(not phase['errors'] and not phase['operation_cap_reached'], 'closed-loop errors/cap')
        for digest, evidence in phase['evidence_exemplars'].items():
            require(hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(',', ':')).encode()).hexdigest() == digest, 'semantic exemplar digest differs')
            oracle.verify(evidence)
    start = int(phase['start_monotonic_ns']) / 1e9
    for row in rows:
        opid = f'{prefix}-{row["id"]}'; allowed.add(opid)
        require(row['outcome'] in ('completed', 'failed', 'admission_drop', 'dispatch_lag_drop', 'drain_timeout'), 'unknown/integrity outcome')
        receipts = native.pop(opid, [])
        if row['outcome'] in ('admission_drop', 'dispatch_lag_drop'):
            require(not receipts and row['observed_s'] >= row['at_s'], 'dropped request was dispatched')
            if row['outcome'] == 'dispatch_lag_drop': require(row['observed_s'] - row['at_s'] > phase['max_dispatch_lag_s'], 'false dispatch drop')
            continue
        require(row['worker'] in owners and row['dispatch_s'] >= 0 and row['dispatch_s'] <= row['complete_s'] <= row['validation_complete_s'], 'logical receipt/validation clocks differ')
        if not serial and not closed: require(row['intended_s'] == row['at_s'] and row['dispatch_s'] >= row['intended_s'], 'offered clock differs')
        steps = row['steps']; require(len(steps) == len(receipts) and steps, 'logical/native step coverage differs')
        for step, receipt in zip(steps, receipts):
            require(all(step[k] == receipt[k if k != 'bytes' else 'body_bytes'] for k in ('status', 'bytes', 'path', 'server_connection_id', 'outcome')), 'logical/native step differs')
        if receipts[-1]['body_receipt_ns'] is not None:
            require(math.isclose(row['complete_s'] + start, int(receipts[-1]['body_receipt_ns']) / 1e9, abs_tol=1e-6, rel_tol=0), 'logical completion differs from final native body receipt')
        require(int(receipts[-1]['validation_finish_ns']) / 1e9 <= start + row['validation_complete_s'] + 1e-6, 'native validation follows logical validation')
        if closed: require(row['outcome'] == 'completed' and row['evidence_sha256'] in phase['evidence_exemplars'], 'qualification read incomplete')
        if row['outcome'] == 'completed':
            evidence = phase['evidence_exemplars'][row['evidence_sha256']] if closed else row['read_evidence']
            fields = {'status': 200, 'path': '/issues' if row['kind'] == 'read' else '/control/mutate'}
            if row['kind'] == 'read': oracle.verify(evidence); fields['snapshot'] = evidence['snapshot']
            else:
                ack = row['acknowledgement']; require(ack['changed'] is True and ack['operation_id'] == opid and type(ack['revision']) is int and ack['revision'] >= 1, 'write acknowledgement differs')
                fields['revision'] = ack['revision']
            required[opid] = fields
    require(not native, 'unassigned native client requests')
    if serial: require(all(a['validation_complete_s'] <= b['dispatch_s'] for a, b in zip(rows, rows[1:])), 'serial requests overlap')
    return rows


def browser(folder, case, spec, oracle, required, allowed, server, cpu, *, label, loaded=False, quiet=False):
    report, ready, completed, config = (load(folder / name) for name in ('loaded-browser.json', 'loaded-ready.json', 'loaded-completed.json', 'loaded-browser-config.json'))
    require(config['read_operation'] == 'discussion_rank' and 'local_correctness' not in config, 'cloud browser scope differs')
    require(report['start_signal'] == load(folder / 'loaded-start.json'), 'browser parent start differs')
    launch = load(folder / 'loaded-launch.json')
    require(launch['pid'] == ready['pid'] and int(launch['before_ns']) <= int(ready['monotonic_ns']) <= int(launch['after_ns']), 'browser launch clock differs')
    if loaded or quiet:
        selected = case if loaded else {**case, 'name': case['name'] + '-quiet'}
        selected_spec = spec if loaded else {**spec, 'duration_s': spec['serial_browser_duration_s'],
            'loaded_browser_offsets_s': [.25, 1.75, 3.25] if spec['serial_browser_duration_s'] == 5 else [.5, 3.5, 6.5]}
        summary = check_loaded(report, ready, completed, config, selected, selected_spec, oracle)
    else:
        require(report['status'] == 'completed' and ready['contexts'] == completed['probes'] == 1 and len(report['probes']) == 1, 'functional browser coverage differs')
        probe = check_browser(report, case, label)
        require(probe['outcome'] == 'completed' and probe['operation_id'] == case['name'] + '-' + label + '-loaded-0', 'functional browser identity differs')
        if label == 'browser-failure': require(probe['app_error'] == 'origin_http_failure' and probe['upstream_status'] == '503', 'browser fault contract differs')
        else: oracle.verify(probe['evidence'])
        summary = {'completed': 1, 'eligible': True}
    for probe in report['probes']:
        allowed.add(probe['operation_id'])
        require(probe.get('error_type') != 'AssertionError' and not any(e['error_type'] == 'AssertionError' for e in probe['response_errors']), 'browser integrity assertion failed')
        if probe['outcome'] == 'completed':
            require(probe['protocols'] and all(p == 'http/1.1' for p in probe['protocols']) and not probe['response_errors'], 'browser native protocol differs')
            required[probe['operation_id']] = {'status': 503 if probe['expected_failure'] else 200, 'path': '/issues',
                **({} if probe['expected_failure'] else {'snapshot': probe['evidence']['snapshot']})}
        for receipt in probe['responses']: reconcile(receipt, server, cpu, browser=True)
    if (loaded or quiet) and summary['completed']: require((folder / 'loaded-browser.png').is_file(), 'browser screenshot missing')
    if not loaded:
        samples = load(folder / 'placement.json'); require(len(samples) == 2, 'browser placement boundary coverage differs')
        for sample in samples: verify_browser_sample(sample, cpu)
    return summary, report['probes']


def audit(target, observer, commit, metadata):
    a, b = load(target / 'report.json'), load(observer / 'report.json'); spec = design(a['smoke']); planned = cases(a['allocation'], a['block'], a['smoke'])
    for role, report in (('target', a), ('observer', b)):
        require(report['status'] == 'passed' and report['role'] == ROLE and report['confirmatory_data'] is False, 'incomplete/wrong cohort')
        require(report['local']['role'] == role and report['local']['source_commit'] == commit and report['design'] == spec and report['cases'] == planned and
            report['allocation'] == a['allocation'] and report['block'] == a['block'] and report['smoke'] == a['smoke'], 'cohort source/matrix differs')
        require(len(report['cells']) == len(planned) and all(all(row.get(k) == v for k, v in case.items()) for row, case in zip(report['cells'], planned)), 'collected panel differs')
    require(a['source_sha256'] == b['source_sha256'] and a['remote'] == b['local'] and b['remote'] == a['local'] and
        a['local']['kernel_boot_sha256'] != b['local']['kernel_boot_sha256'], 'reciprocal/source/kernel identity differs')
    for name, digest in a['source_sha256'].items():
        raw = subprocess.run(['git', 'show', f'{commit}:{name}'], cwd=ROOT, check=True, capture_output=True).stdout
        require(hashlib.sha256(raw).hexdigest() == digest, 'source bytes differ: ' + name)
    require(str(metadata['databaseId']) == a['local']['run_id'] and metadata['headSha'] == commit and metadata['conclusion'] == 'success' and
        len(metadata['jobs']) == 3 and all(j['conclusion'] == 'success' for j in metadata['jobs']), 'successful paired run metadata differs')
    cleanup = [s for j in metadata['jobs'] for s in j['steps'] if s['name'] == 'Post Join dedicated research overlay with ephemeral workload identity']
    require(len(cleanup) == 2 and all(s['conclusion'] == 'success' for s in cleanup), 'ephemeral overlay cleanup incomplete')
    require(not a['gateway_errors'], 'gateway control errors present')
    for filename, minimum in (('preflight.json', 85), ('wal-preflight.json', 1), ('endtoend-preflight.json', 8)):
        value = load(target.parent / filename)
        require(value['status'] == 'passed' and value['run'] >= minimum and value['skipped'] == value['errors'] == value['failures'] == 0, 'target preflight incomplete: ' + filename)
    cpu_t, cpu_o = sorted(a['local']['allowed_cpus'])[:2], sorted(b['local']['allowed_cpus'])[:2]
    require(len(cpu_t) == len(cpu_o) == 2, 'two logical CPUs required')
    containment = load(observer.parent / 'browser-cgroups.json')['groups']['observer']
    require(containment['path'] == '/sys/fs/cgroup/qrl_browser_observer' and containment['cpu'] == cpu_o[1] and containment['effective_cpus'] == str(cpu_o[1]), 'kernel browser cpuset differs')
    check_clock(load(observer / 'browser-clock.json')); check_clock(load(observer.parent / 'browser-clock-contract.json'))
    server = server_connections(load(target / 'connection-observations.json'), cpu_t[0]); cells = []; totals = Counter(); qualifications = []
    for case, reported in zip(planned, b['cells']):
        t, o = target / case['name'], observer / case['name']; state = load(t / 'validation-state.json')
        require((state['rows'], state['fanout'], state['query_kind'], state['distribution']['family'], state['distribution']['seed']) ==
            (case['rows'], 5, case['query_kind'], case['family'], case['seed']), 'fixture identity differs')
        oracle = RankedOracle(state); final = oracle.verify_final(state); required = {}; allowed = set(); records = []; acknowledged = set(); expected = {}
        database = load(t / 'database.json'); before = database['service']; after = load(t / 'database-after.json')
        for diagnostics in (before, after):
            require(diagnostics['resource_policy'] == 'ranked-resource-v1' and diagnostics['pool_size'] == diagnostics['pool_connections'] == 4 and
                diagnostics['prepare_threshold'] is None and diagnostics['statement_timeout'] == '10s' and diagnostics['write_phase_tracing'] is False and
                diagnostics['durable_writes'] is True and all(diagnostics['durability'][k] == 'on' for k in ('synchronous_commit', 'fsync', 'full_page_writes')), 'pool/durability/timeout differs')
        require(before['read_query_sha256'] == after['read_query_sha256'] == database['ranking']['read_query_sha256'], 'selected SQL identity differs')
        if case['ranking_design']: require(state['ranking_design']['design'] == case['ranking_design'], 'maintained query shape differs')
        warm = load(o / 'warmup.json.gz'); bundle(warm, case['name'] + '-warm', cpu_o[0], server, oracle, required, allowed, serial=True)
        require(len(warm['operations']) == 4 and all(r['outcome'] == 'completed' and r['kind'] == 'read' for r in warm['operations']), 'warmup coverage differs')
        windows = load(t / 'windows.json')
        if case['phase'] == 'qualification':
            expected_windows = [(f'pair-{p}-' + ('on' if e else 'off'), e) for p in range(3) for e in qualification_order(a['allocation'], a['block'], int(case['name'].split('-')[1]), p)]
            require([(w['label'], w['instrumented']) for w in windows] == expected_windows, 'qualification window order differs')
            pairs = []
            for pair in range(3):
                counts = {}
                for enabled in (False, True):
                    label = f'pair-{pair}-' + ('on' if enabled else 'off'); value = load(o / (label + '.json.gz'))
                    require(value['phase']['duration_s'] == 5 and value['phase']['workers'] == 4, 'qualification duration/workers differ')
                    rows = bundle(value, case['name'] + '-' + label, cpu_o[0], server, oracle, required, allowed, closed=True)
                    counts[enabled] = sum(r['complete_s'] <= 5 for r in rows); totals['qualification_operations'] += len(rows)
                    if not enabled: require(not (o / (label + '-budget.json')).exists() and not (t / (label + '-budget.json')).exists(), 'disabled CPU observation has samples')
                require(counts[False] > 0 and counts[True] > 0, 'empty qualification window')
                pairs.append({'pair': pair, 'enabled': counts[True], 'disabled': counts[False], 'ratio': counts[True] / counts[False]})
            result = {'case': case['name'], 'pairs': pairs, 'median_ratio': statistics.median(p['ratio'] for p in pairs)}
            require(result == load(o / 'qualification.json') == reported['summary'] and result['median_ratio'] >= .90, 'observation cost qualification differs/fails')
            qualifications.append(result); summary = {'qualification': result}
        else:
            require(len(qualifications) == 8 and windows and [(w['label'], w['instrumented']) for w in windows] == [('main', True)], 'main window/qualification gate differs')
            plan = trace(case, a['allocation'], a['smoke']) if case['phase'] == 'loaded' else schedule(1, spec['serial_operations'], .4, case['rows'], SEED + a['allocation'])
            require(plan == load(o / 'schedule.json'), 'planned arrivals differ')
            value = load(o / 'operations.json.gz'); records = bundle(value, case['name'] + '-main', cpu_o[0], server, oracle, required, allowed, plan=plan, serial=case['phase'] == 'serial')
            require(value['phase']['workers'] == (16 if case['phase'] == 'loaded' else 1), 'main worker budget differs')
            if case['phase'] == 'loaded': require(value['phase']['max_dispatch_lag_s'] == .05 and value['phase']['drain_timeout_s'] == 30, 'dispatch/drain bounds differ')
            for row in records:
                if row['kind'] == 'write':
                    key = f'{case["name"]}-main-{row["id"]}'; expected[key] = {'issue_id': row['issue_id'], 'kind': row['mutation'], 'value': row['value']}
                    if row['outcome'] == 'completed': acknowledged.add(key)
            fault = load(o / 'logical-failure.json.gz'); bundle(fault, case['name'] + '-fault', cpu_o[0], server, oracle, required, allowed, serial=True)
            require(len(fault['operations']) == 1 and fault['operations'][0]['outcome'] == 'failed' and fault['operations'][0]['upstream_status'] == 503, 'logical fault contract differs')
            required[case['name'] + '-fault-0'] = {'status': 503, 'path': '/issues'}
            probes = {}
            for label in ('browser-before', 'browser-failure', 'browser-after'):
                _, raw = browser(o / label, case, spec, oracle, required, allowed, server, cpu_o[1], label=label); probes[label] = raw[0]
            control = load(t / 'visible-control.json'); payload = control['mutation']; key = payload['operation_id']
            require(key == case['name'] + '-visible-comment' and payload['kind'] == 'comment', 'visible control identity differs')
            expected[key] = {k: payload[k] for k in ('issue_id', 'kind', 'value')}; acknowledged.add(key); allowed.add(key)
            required[key] = {'status': 200, 'path': '/control/mutate', 'revision': control['ack']['revision']}
            prior = {**state, 'ledger': [r for r in state['ledger'] if r['operation_id'] != key]}
            require(RankedOracle(prior).issue_at(payload['issue_id']) == control['before'] and control['ack']['changed'] is True and
                control['ack']['revision'] == control['before']['revision'] + 1, 'visible control state/ack differs')
            first = probes['browser-after']['evidence']['issues'][0]
            require(first['id'] == payload['issue_id'] and first['comment_count'] == control['before']['comment_count'] + 1, 'ranked write not visible')
            label = 'loaded' if case['phase'] == 'loaded' else 'quiet'
            browser_summary, _ = browser(o / label, case, spec, oracle, required, allowed, server, cpu_o[1], label=label, loaded=label == 'loaded', quiet=label == 'quiet')
            start = int(value['phase']['start_monotonic_ns']); finish = int(value['phase']['finish_monotonic_ns'])
            if label == 'loaded': require(load(o / label / 'loaded-start.json')['measurement_start_monotonic_ns'] == value['phase']['start_monotonic_ns'], 'HTTP/browser start clocks differ')
            else: require(finish <= int(load(o / label / 'loaded-launch.json')['before_ns']), 'serial HTTP/quiet browser phases overlap')
            for window in ('pre', 'post'):
                paths = load(o / ('path-' + window + '.json')); receipts = paths['records']
                require(paths['http_policy'] == POLICY and len(receipts) == (3 if a['smoke'] else 16), 'path control coverage differs')
                require((int(paths['finish_monotonic_ns']) <= start) if window == 'pre' else (int(paths['start_monotonic_ns']) >= finish), 'path controls overlap HTTP window')
                for i, receipt in enumerate(receipts):
                    reconcile(receipt, server, cpu_o[0]); raw = body_for(identity(a['local']['run_id'], case['name'], window, i))
                    require(receipt['operation_id'] == f'{case["name"]}-path-{window}-{i}' and receipt['status'] == 200 and
                        receipt['body_bytes'] == 1024 and receipt['body_sha256'] == hashlib.sha256(raw).hexdigest(), 'path body identity differs')
            normalized = records if case['phase'] == 'loaded' else [{**r, 'intended_s': r['dispatch_s']} for r in records]
            seconds = spec['duration_s'] if case['phase'] == 'loaded' else (finish - start) / 1e9
            summary = {'http': summarize(normalized, seconds), 'classes': {kind: summarize([r for r in normalized if ('read' if r['kind'] == 'read' else r['mutation']) == kind], seconds) for kind in ('read', 'comment', 'state')}, 'browser': browser_summary}
            require(reported['summary'] == {'offered': len(records), 'outcomes': dict(Counter(r['outcome'] for r in records))}, 'reported main outcomes differ')
            totals[case['phase'] + '_offered'] += len(records); totals[case['phase'] + '_completed'] += sum(r['outcome'] == 'completed' for r in records)
            totals[case['phase'] + '_browser_probes'] += 3
        actual = {r['operation_id']: {k: r[k] for k in ('issue_id', 'kind', 'value')} for r in state['ledger']}
        require(acknowledged.issubset(actual) and all(k in expected and v == expected[k] for k, v in actual.items()), 'committed mutation identities/payloads differ')
        ambiguous = sorted(set(actual) - acknowledged); totals['ambiguous_writes'] += len(ambiguous)
        events = [json.loads(line) for line in (t / 'backend.jsonl').read_text().splitlines()]; verify_backend_coverage(events, required, allowed)
        for event in events:
            require(event['query_kind'] == case['query_kind'], 'backend query kind differs')
            if event['path'] == '/issues': require(event['read_query_sha256'] == before['read_query_sha256'], 'backend selected SQL differs')
            if event['status'] == 200:
                require(event['backend'] is not None and all(type(event['backend'][k]) in (float, int) and math.isfinite(event['backend'][k]) and event['backend'][k] >= 0 for k in ('pool_wait_ms', 'transaction_wall_ms')), 'backend native clock missing/invalid')
        for index, window in enumerate(windows):
            require(int(window['start_monotonic_ns']) < int(window['finish_monotonic_ns']) and (index == 0 or int(windows[index - 1]['finish_monotonic_ns']) < int(window['start_monotonic_ns'])), 'target windows overlap')
            label = window['label']; wal = delta(load(t / (label + '-wal-before.json')), load(t / (label + '-wal-after.json')), False)
            pids = {r['pid'] for r in wal['backends']}
            require(all(e['backend']['backend_pid'] in pids for e in events if e['status'] == 200), 'request used non-pool backend')
            if window['instrumented']:
                check_budget(load(t / (label + '-budget.json')), {'gateway_controller': cpu_t[:1], 'overlay': cpu_t[:1], 'origin': cpu_t[:1], 'web': cpu_t[:1], 'postgres': cpu_t[1:2]})
                observed = load(o / (label + '-budget.json')); expected_groups = {'generator_sampler': cpu_o[:1], 'overlay': cpu_o[:1]}
                if case['phase'] == 'loaded': expected_groups['loaded_browser'] = cpu_o[1:2]
                check_budget(observed, expected_groups)
                if case['phase'] == 'loaded':
                    for sample in observed['samples']: verify_browser_sample(sample['groups']['loaded_browser'], cpu_o[1])
        for folder, report in ((t, a), (o, b)):
            routes = load(folder / 'routes.json'); verify_routes(routes)
            for row in routes:
                require(row['network']['self']['HostName'] == report['local']['overlay_name'] and row['network']['peer']['HostName'] == report['remote']['overlay_name'], 'route peer identity differs')
        require(final == a['cells'][len(cells)]['final_oracle'], 'target final oracle projection differs')
        cells.append({**case, **summary, 'final_oracle': final, 'ambiguous_writes': ambiguous,
            'backend': backend_summaries(records, events, case['name'] + '-main') if records else None})
    require(qualifications == load(observer / 'qualification.json'), 'complete qualification projection differs')
    return {'status': 'passed', 'role': ROLE, 'confirmatory_data': False, 'source_commit': commit, 'run_id': a['local']['run_id'],
        'allocation': a['allocation'], 'block': a['block'], 'smoke': a['smoke'], 'design': spec, 'cells': cells, 'totals': dict(totals),
        'source_files_checked': len(a['source_sha256']), 'host_kernels': [r['local']['kernel_boot_sha256'] for r in (a, b)],
        'auditor_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'scope': 'Finite development bridge. All-offered outcomes retained; serial and loaded phases separate; no fitted selector or main/held-out estimate.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('target', 'observer', 'run-metadata', 'output'): parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--commit', required=True); args = parser.parse_args()
    result = audit(args.target, args.observer, args.commit, load(args.run_metadata)); args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n'); print(json.dumps({'status': result['status'], 'totals': result['totals']}))


if __name__ == '__main__': main()
