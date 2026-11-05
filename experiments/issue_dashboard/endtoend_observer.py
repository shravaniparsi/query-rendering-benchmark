"""Qualification, serial profiles, and complete loaded panels on the observer VM."""
import gzip
import json
import os
from pathlib import Path
import statistics
import time

from .arrivals import schedule
from .cpu_budget import bind_current, bind_overlay, overlay_pid, tree
from .endtoend_design import design, cases, trace, qualification_order, SEED
from .endtoend_transport import ReceiptSession, IssueClient, POLICY
from .loaded_browser import LoadedBrowser
from .offhost_engineering import RouteWatch, require, request_json, save, verify_routes
from .offhost_resources import BudgetWatch
from .owned_arrivals import execute_owned, execute_serial_owned, execute_closed_owned
from .path_controls import body_for, identity
from scripts.offhost_connectivity import network_state, check_pair


def compact(path, value):
    with gzip.GzipFile(filename=str(path), mode='wb', mtime=0) as stream:
        stream.write((json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode())


def client_factory(peer_ip, case, suffix, cpus):
    return lambda _: IssueClient(ReceiptSession(peer_ip, 8765, persistent=True, expected_cpus=cpus),
        case['strategy'], case['name'] + '-' + suffix)


def browser(base, directory, case, cpu, spec, label, *, failure=False, profile=False):
    folder = directory / label; folder.mkdir()
    settings = {**spec, 'duration_s': spec['serial_browser_duration_s'] if profile else 1,
        'loaded_browser_offsets_s': ([.25, 1.75, 3.25] if spec['serial_browser_duration_s'] == 5 else [.5, 3.5, 6.5]) if profile else [.05],
        'loaded_browser_max_dispatch_lag_s': .1}
    instance = LoadedBrowser(base, folder, {**case, 'name': case['name'] + '-' + label}, cpu, settings,
        script=Path(__file__).with_name('endtoend_browser.cjs'))
    instance.config.update(read_operation='discussion_rank', failure=failure)
    samples = []
    try:
        instance.launch(); samples.append(instance.sample()); started = time.perf_counter(); instance.start(started); instance.wait_completed()
        left = settings['duration_s'] - (time.perf_counter() - started)
        if left > 0: time.sleep(left)
        samples.append(instance.sample()); value = instance.release()
        require(all(r['outcome'] == 'completed' and r['checks_passed'] for r in value['probes']), 'functional/quiet browser checks failed')
        return value
    finally:
        instance.close(); save(folder / 'placement.json', samples)


def path_controls(peer_ip, directory, case, window, own, smoke, cpus):
    session = ReceiptSession(peer_ip, 8765, persistent=True, expected_cpus=cpus); start = time.perf_counter_ns()
    try:
        for index in range(3 if smoke else 16):
            value = identity(own['run_id'], case['name'], window, index)
            row, raw = session.request('GET', f'/qrl/path-probe/{case["name"]}/{window}/{index}/{own["run_id"]}',
                operation_id=f'{case["name"]}-path-{window}-{index}')
            require(row['outcome'] == 'completed' and row['status'] == 200 and raw == body_for(value), 'owned path control differs')
    finally:
        session.close(); save(directory / ('path-' + window + '.json'), {'http_policy': POLICY,
            'body_format': 'matched-gateway-path-v1', 'window': window, 'start_monotonic_ns': str(start),
            'finish_monotonic_ns': str(time.perf_counter_ns()), 'records': session.records,
            'feature_scope': 'outside application windows; no subtraction from application times'})


def run_observer(output, own, peer_name, peer_ip, allocation, block, smoke, collected):
    spec = design(smoke); values = cases(allocation, block, smoke); cpus = sorted(own['allowed_cpus'])[:2]
    require(len(cpus) == 2, 'two observer CPUs required'); bind_current(cpus[:1]); overlay = overlay_pid(); bind_overlay(overlay, cpus[:1])
    base = f'http://{peer_ip}:8765'; deadline = time.monotonic() + 180
    while True:
        try: remote = request_json(base, '/manifest', timeout=3); break
        except OSError:
            if time.monotonic() > deadline: raise
            time.sleep(1)
    check_pair(own, remote); request_json(base, '/qrl/hello', own)
    def capture(loaded=None):
        value = {'generator_sampler': tree(os.getpid(), cpus[:1], False), 'overlay': tree(overlay, cpus[:1], False)}
        if loaded: value['loaded_browser'] = loaded.sample()
        return value
    qualification = []
    for case in values:
        directory = output / case['name']; directory.mkdir(); loaded = None; budget = None
        require(request_json(base, '/qrl/begin', {'case': case['name'], 'observer': own}, timeout=120) == {'case': case, 'ready': True}, 'begin differs')
        network_state(own['overlay_name'], peer_name, timeout=30, require_direct=True)
        routes = RouteWatch(own['overlay_name'], peer_name).start()
        try:
            warm = [{'id': i, 'kind': 'read', 'project_id': 0, 'state': 'open', 'limit': 20, 'offset': 0} for i in range(spec['warmup_reads'])]
            rows, native, phase = execute_serial_owned(warm, client_factory(peer_ip, case, 'warm', cpus[:1]))
            compact(directory / 'warmup.json.gz', {'operations': rows, 'sessions': native, 'phase': phase})
            require(all(r['outcome'] == 'completed' for r in rows), 'warmup incomplete')
            if case['phase'] == 'qualification':
                pairs = []
                for pair in range(3):
                    counts = {}
                    for enabled in qualification_order(allocation, block, int(case['name'].split('-')[1]), pair):
                        label = f'pair-{pair}-' + ('on' if enabled else 'off')
                        request_json(base, '/qrl/window-begin', {'case': case['name'], 'label': label, 'instrumented': enabled})
                        budget = BudgetWatch(lambda: capture()).start() if enabled else None
                        try:
                            rows, native, phase = execute_closed_owned(client_factory(peer_ip, case, label, cpus[:1]),
                                seconds=spec['overhead']['window_s'], workers=4, max_operations=spec['overhead']['max_operations_per_window'])
                        finally:
                            if budget: save(directory / (label + '-budget.json'), budget.finish()); budget = None
                        request_json(base, '/qrl/window-end', {'case': case['name'], 'label': label})
                        compact(directory / (label + '.json.gz'), {'operations': rows, 'sessions': native, 'phase': phase})
                        require(not phase['errors'] and not phase['operation_cap_reached'] and rows and all(r['outcome'] == 'completed' for r in rows), 'qualification native work incomplete/capped')
                        counts[enabled] = sum(r['complete_s'] <= spec['overhead']['window_s'] for r in rows)
                    require(counts[False] > 0 and counts[True] > 0, 'empty qualification window')
                    pairs.append({'pair': pair, 'enabled': counts[True], 'disabled': counts[False], 'ratio': counts[True] / counts[False]})
                result = {'case': case['name'], 'pairs': pairs, 'median_ratio': statistics.median(p['ratio'] for p in pairs)}
                save(directory / 'qualification.json', result); qualification.append(result)
                require(result['median_ratio'] >= spec['overhead']['minimum_context_median_ratio'], 'CPU observation overhead exceeds qualification')
                summary = result
            else:
                require(len(qualification) == 8, 'all query/renderer observation costs must qualify before profiles/load')
                browser(base, directory, case, cpus[1], spec, 'browser-before')
                request_json(base, '/qrl/fault-on', {'case': case['name']})
                browser(base, directory, case, cpus[1], spec, 'browser-failure', failure=True)
                fault, native, phase = execute_serial_owned(warm[:1], client_factory(peer_ip, case, 'fault', cpus[:1]))
                compact(directory / 'logical-failure.json.gz', {'operations': fault, 'sessions': native, 'phase': phase})
                require(fault[0]['outcome'] == 'failed' and fault[0]['upstream_status'] == 503, 'logical fault not visible')
                request_json(base, '/qrl/fault-off', {'case': case['name']})
                path_controls(peer_ip, directory, case, 'pre', own, smoke, cpus[:1])
                if case['phase'] == 'loaded':
                    plan = trace(case, allocation, smoke); folder = directory / 'loaded'; folder.mkdir()
                    loaded = LoadedBrowser(base, folder, case, cpus[1], spec, script=Path(__file__).with_name('endtoend_browser.cjs'))
                    loaded.config['read_operation'] = 'discussion_rank'; loaded.launch()
                else:
                    plan = schedule(1, spec['serial_operations'], spec['serial_write_fraction'], case['rows'], SEED + allocation)
                save(directory / 'schedule.json', plan)
                request_json(base, '/qrl/window-begin', {'case': case['name'], 'label': 'main', 'instrumented': True})
                budget = BudgetWatch(lambda: capture(loaded)).start()
                try:
                    if loaded:
                        rows, native, phase = execute_owned(plan, client_factory(peer_ip, case, 'main', cpus[:1]), workers=16,
                            max_dispatch_lag=spec['max_dispatch_lag_s'], drain_timeout=spec['drain_timeout_s'], start_callback=loaded.start)
                        left = spec['duration_s'] - (time.perf_counter_ns() - int(phase['start_monotonic_ns'])) / 1e9
                        if left > 0: time.sleep(left)
                        loaded.wait_completed()
                    else: rows, native, phase = execute_serial_owned(plan, client_factory(peer_ip, case, 'main', cpus[:1]))
                    compact(directory / 'operations.json.gz', {'operations': rows, 'sessions': native, 'phase': phase})
                finally:
                    save(directory / 'main-budget.json', budget.finish()); budget = None
                request_json(base, '/qrl/window-end', {'case': case['name'], 'label': 'main'})
                require(phase['workers_joined'] and not any(r['outcome'] == 'integrity_failure' for r in rows), 'worker drain or semantic contract failure')
                if loaded: loaded.release()
                else: browser(base, directory, case, cpus[1], spec, 'quiet', profile=True)
                path_controls(peer_ip, directory, case, 'post', own, smoke, cpus[:1])
                request_json(base, '/qrl/visible-write', {'case': case['name']}); browser(base, directory, case, cpus[1], spec, 'browser-after')
                summary = {'offered': len(rows), 'outcomes': {kind: sum(r['outcome'] == kind for r in rows) for kind in sorted({r['outcome'] for r in rows})}}
        finally:
            if budget: save(directory / 'interrupted-budget.json', budget.finish())
            if loaded: loaded.close()
            samples = routes.finish(); save(directory / 'routes.json', samples)
        verify_routes(samples); require(request_json(base, '/qrl/end', {'case': case['name']}, timeout=120) == {'closed': True}, 'end differs')
        collected.append({**case, 'summary': summary}); print(json.dumps({'phase': 'cell_complete', **case, 'summary': summary}), flush=True)
    request_json(base, '/qrl/finish', {'observer': own}); save(output / 'qualification.json', qualification)
    return remote
