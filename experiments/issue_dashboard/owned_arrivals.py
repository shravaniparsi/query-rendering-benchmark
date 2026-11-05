"""Open-loop bounded admission with one persistent client owned and closed by each worker."""
import math
import hashlib
import json
import queue
import threading
import time


def execute_owned(plan, factory, *, workers=16, max_dispatch_lag=.05, drain_timeout=30, start_callback=None):
    if type(workers) is not int or not 1 <= workers <= 16 or not math.isfinite(max_dispatch_lag) or max_dispatch_lag < 0 or not math.isfinite(drain_timeout) or drain_timeout <= 0:
        raise ValueError('generator bounds differ')
    if len({r['id'] for r in plan}) != len(plan) or any(r['at_s'] < 0 for r in plan) or any(a['at_s'] > b['at_s'] for a, b in zip(plan, plan[1:])):
        raise ValueError('ordered distinct arrivals required')
    slots = threading.BoundedSemaphore(workers); work_queue = queue.Queue(); lock = threading.Lock()
    records = {}; sessions = {}; submitted = {}; threads = []; start = None; sealed = False
    ready = threading.Barrier(workers + 1)
    def work(index):
        client = None; ready.wait()
        try:
            while True:
                item = work_queue.get()
                if item is None: break
                dispatched = time.perf_counter() - start
                try:
                    if client is None: client = factory(index)
                    value = client.invoke(item); validated = time.perf_counter()
                    received = value.pop('_response_completed_monotonic', None) or validated
                    row = {**item, **value, 'worker': index, 'intended_s': item['at_s'], 'dispatch_s': dispatched,
                        'complete_s': received - start, 'validation_complete_s': validated - start}
                except Exception as error:
                    row = {**item, 'worker': index, 'intended_s': item['at_s'], 'dispatch_s': dispatched,
                        'complete_s': time.perf_counter() - start, 'outcome': 'integrity_failure', 'error_type': type(error).__name__}
                finally: slots.release()
                with lock:
                    if not sealed: records[item['id']] = row
        finally:
            if client is not None:
                client.session.close()
                with lock: sessions[index] = {'owner_thread': threading.get_ident(), 'records': client.session.records}
    for index in range(workers):
        thread = threading.Thread(target=work, args=(index,), daemon=True, name=f'qrl-owned-{index}'); thread.start(); threads.append(thread)
    ready.wait(); start = time.perf_counter()
    try:
        if start_callback is not None: start_callback(start)
        for item in plan:
            remaining = start + item['at_s'] - time.perf_counter()
            if remaining > 0: time.sleep(remaining)
            observed = time.perf_counter() - start
            if observed - item['at_s'] > max_dispatch_lag:
                records[item['id']] = {**item, 'intended_s': item['at_s'], 'observed_s': observed, 'outcome': 'dispatch_lag_drop'}
            elif not slots.acquire(blocking=False):
                records[item['id']] = {**item, 'intended_s': item['at_s'], 'observed_s': observed, 'outcome': 'admission_drop'}
            else:
                submitted[item['id']] = item; work_queue.put(item)
    finally:
        for _ in threads: work_queue.put(None)
    deadline = start + (plan[-1]['at_s'] if plan else 0) + drain_timeout
    for thread in threads: thread.join(max(0, deadline - time.perf_counter()))
    with lock:
        sealed = True
        for key, item in submitted.items():
            if key not in records: records[key] = {**item, 'intended_s': item['at_s'], 'observed_s': time.perf_counter() - start, 'outcome': 'drain_timeout'}
        session_rows = [{'worker': i, **v} for i, v in sorted(sessions.items())]
    phase = {'start_monotonic_ns': str(round(start * 1e9)), 'finish_monotonic_ns': str(time.perf_counter_ns()),
        'workers': workers, 'workers_joined': all(not thread.is_alive() for thread in threads),
        'max_dispatch_lag_s': max_dispatch_lag, 'drain_timeout_s': drain_timeout, 'attempts_per_http_request': 1}
    return sorted(records.values(), key=lambda r: r['id']), session_rows, phase


def execute_serial_owned(plan, factory):
    """One client, no overlapping logical requests; retain native receipt clocks."""
    client = factory(0); records = []; start = time.perf_counter()
    try:
        for item in plan:
            dispatched = time.perf_counter(); value = client.invoke(item); validated = time.perf_counter()
            received = value.pop('_response_completed_monotonic', None) or validated
            records.append({**item, **value, 'worker': 0, 'dispatch_s': dispatched - start,
                'complete_s': received - start, 'validation_complete_s': validated - start})
    finally: client.session.close()
    return records, [{'worker': 0, 'owner_thread': threading.get_ident(), 'records': client.session.records}], {
        'start_monotonic_ns': str(round(start * 1e9)), 'finish_monotonic_ns': str(time.perf_counter_ns()),
        'workers': 1, 'workers_joined': True, 'attempts_per_http_request': 1}


def execute_closed_owned(factory, *, seconds=5, workers=4, max_operations=50000):
    """Read-only qualification. Both modes perform identical receipt/hash checks.

    Repeated semantic payloads are stored once by content digest. Every operation
    retains its own outcome, clock, evidence reference, and native HTTP receipts.
    """
    if not math.isfinite(seconds) or seconds <= 0 or type(workers) is not int or not 1 <= workers <= 16 or type(max_operations) is not int or max_operations < workers:
        raise ValueError('closed-loop bounds differ')
    ready = threading.Barrier(workers + 1); lock = threading.Lock(); rows = []; sessions = []; exemplars = {}; counter = 0; errors = []
    start = None
    def work(index):
        nonlocal counter
        client = None; local = []
        try:
            ready.wait()
            client = factory(index)
            while time.perf_counter() < start + seconds:
                with lock:
                    if counter >= max_operations: break
                    key = counter; counter += 1
                item = {'id': key, 'kind': 'read', 'project_id': 0, 'state': 'open', 'limit': 20, 'offset': 0}
                dispatched = time.perf_counter(); value = client.invoke(item); validated = time.perf_counter()
                received = value.pop('_response_completed_monotonic', None) or validated
                evidence = value.pop('read_evidence', None); digest = None
                if evidence is not None:
                    digest = hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                    with lock: exemplars[digest] = evidence
                local.append({**item, **value, 'evidence_sha256': digest, 'worker': index,
                    'dispatch_s': dispatched - start, 'complete_s': received - start, 'validation_complete_s': validated - start})
        except Exception as error:
            with lock: errors.append({'worker': index, 'error_type': type(error).__name__})
        finally:
            if client is not None: client.session.close()
            with lock:
                rows.extend(local)
                if client is not None: sessions.append({'worker': index, 'owner_thread': threading.get_ident(), 'records': client.session.records})
    threads = [threading.Thread(target=work, args=(i,), daemon=True) for i in range(workers)]
    for thread in threads: thread.start()
    start = time.perf_counter(); ready.wait()
    deadline = start + seconds + 30
    for thread in threads: thread.join(max(0, deadline - time.perf_counter()))
    if any(thread.is_alive() for thread in threads): raise RuntimeError('closed-loop worker did not drain')
    return sorted(rows, key=lambda r: r['id']), sorted(sessions, key=lambda r: r['worker']), {
        'start_monotonic_ns': str(round(start * 1e9)), 'finish_monotonic_ns': str(time.perf_counter_ns()),
        'duration_s': seconds, 'workers': workers, 'workers_joined': True,
        'operation_cap_reached': counter >= max_operations, 'attempts_per_http_request': 1,
        'evidence_exemplars': exemplars, 'errors': errors}
