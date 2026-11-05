"""Open-loop logical-operation engineering generator; not browser population emulation.

SSR reads request HTML. CSR reads request the shell followed by the data API.
Writes use the loopback engineering service identically for both modes.
"""
import json
import math
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
from html.parser import HTMLParser
from .fixture import TAGS


def trace(rate, duration, write_fraction=0, seed=0, rows=1000):
    if not all(math.isfinite(x) for x in (rate, duration, write_fraction)):
        raise ValueError('finite schedule parameters required')
    if rate <= 0 or duration <= 0 or not 0 <= write_fraction <= 1 or type(rows) is not int or rows < 40:
        raise ValueError('invalid schedule')
    count = math.floor(rate*duration)
    if count > 1_000_000:
        raise ValueError('engineering trace too large')
    rng = random.Random(seed)
    writes = set(rng.sample(range(count), round(count*write_fraction)))
    if len(writes) > rows:
        raise ValueError('unique-entity write trace exceeds fixture size')
    article_ids = iter(rng.sample(range(rows), len(writes)))
    schedule = []
    write_index = 0
    for i in range(count):
        item = {'id': i, 'at_s': i/rate, 'kind': 'write' if i in writes else 'read'}
        if i in writes:
            article_id = next(article_ids)
            field = 'tag' if write_index % 2 == 0 else 'title'
            write_index += 1
            item.update(article_id=article_id, field=field,
                        value=TAGS[(article_id % len(TAGS)+1) % len(TAGS)] if field=='tag' else f'Updated note {i:07d}')
        schedule.append(item)
    return schedule


class LogicalFailure(Exception):
    def __init__(self, code, upstream_status=None):
        self.code, self.upstream_status = code, upstream_status


class OutcomeParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.outcomes = []
        self.articles=[]; self.current=None; self.field=None; self.count_text=''

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag=='main' and 'data-qrl-outcome' in attrs:
            self.outcomes.append(attrs)
        if tag=='article':
            self.current={'id':int(attrs['data-id']),'revision':int(attrs['data-revision']),
                          'title':'','author':'','tag':''}
        if tag=='h2' and self.current is not None:
            self.field='title'
        if tag=='p' and self.current is not None:
            if 'data-author' in attrs: self.field='author'
            elif 'data-tag' in attrs: self.field='tag'
        if tag=='output' and attrs.get('id')=='article-count':
            self.field='count'

    def handle_data(self,data):
        if self.field=='count': self.count_text+=data
        elif self.field is not None and self.current is not None: self.current[self.field]+=data

    def handle_endtag(self,tag):
        if tag in ('h2','p','output'): self.field=None
        if tag=='article' and self.current is not None:
            self.articles.append(self.current); self.current=None


def page_outcome(body, expected):
    parser = OutcomeParser()
    parser.feed(body.decode('utf-8'))
    if len(parser.outcomes) != 1:
        raise LogicalFailure('invalid_page_outcome')
    attrs = parser.outcomes[0]
    if attrs['data-qrl-outcome']=='failed':
        status = attrs.get('data-upstream-status')
        raise LogicalFailure(attrs.get('data-qrl-error','origin_failure'), int(status) if status else None)
    if attrs['data-qrl-outcome']!=expected:
        raise LogicalFailure('invalid_page_outcome')
    if expected=='completed':
        return {'snapshot':attrs['data-snapshot'],'articles':parser.articles,'articlesCount':int(parser.count_text),
                'tag':'rendering','offset':0,'limit':20}


def logical_operation(operation, strategy, app_url, origin_url, run_id, timeout=5):
    if strategy not in ('ssr', 'csr'):
        raise ValueError('unknown rendering strategy')
    # This runner is deliberately limited to local engineering endpoints.
    for base in (app_url, origin_url):
        if urlparse(base).hostname not in ('127.0.0.1', 'localhost', '::1'):
            raise ValueError('engineering generator requires loopback endpoints')
    steps = []; read_evidence=None; response_clock=None
    def request(url, payload=None):
        nonlocal response_clock
        response_clock=None
        headers = {'Cache-Control': 'no-cache', 'X-QRL-Operation': f'{run_id}-{operation["id"]}'}
        if payload is not None:
            headers.update({'X-QRL-Control': 'engineering', 'Content-Type': 'application/json'})
        req = Request(url, data=None if payload is None else json.dumps(payload).encode(), headers=headers)
        try:
            with urlopen(req, timeout=timeout) as response:
                body = response.read()
                response_clock=time.perf_counter()
                steps.append({'status': response.status, 'bytes': len(body),
                              'request_id': response.headers.get('X-Request-ID')})
                return response.status, body
        except HTTPError as error:
            body = error.read()
            response_clock=time.perf_counter()
            steps.append({'status': error.code, 'bytes': len(body)})
            return error.code, body
    try:
        if operation['kind'] == 'write':
            field = operation['field']
            status, body = request(origin_url + '/control/update', {
                'operation_id': f'{run_id}-{operation["id"]}', 'article_id': operation['article_id'],
                'field': field, 'value': operation['value']})
            ack = json.loads(body)
            if (status != 200 or ack.get('changed') is not True or type(ack.get('revision')) is not int or
                    ack['revision']!=1 or ack.get('operation_id')!=f'{run_id}-{operation["id"]}'):
                raise LogicalFailure('invalid_write_ack',status)
        else:
            query = urlencode({'tag': 'rendering', 'offset': 0})
            status, body = request(f'{app_url}/feed/{strategy}?{query}')
            if status!=200:
                raise LogicalFailure('page_http_failure',status)
            read_evidence=page_outcome(body, 'completed' if strategy=='ssr' else 'loading')
            if strategy == 'csr':
                status, body = request(f'{app_url}/api/feed?{query}')
                result = json.loads(body)
                if result.get('ok') is not True:
                    error = result.get('error',{})
                    raise LogicalFailure(error.get('code','invalid_api_outcome'),error.get('upstream_status'))
                if status!=200 or not isinstance(result.get('articles'),list) or type(result.get('articlesCount')) is not int:
                    raise LogicalFailure('invalid_api_outcome')
                read_evidence={key:result[key] for key in ('snapshot','articles','articlesCount')}
                read_evidence.update(tag='rendering',offset=0,limit=20)
        return {'outcome': 'completed', 'steps': steps,'read_evidence':read_evidence,
                '_response_completed_monotonic':response_clock}
    except LogicalFailure as error:
        return {'outcome':'failed', 'failure_code':error.code, 'upstream_status':error.upstream_status, 'steps':steps,
                '_response_completed_monotonic':response_clock}
    except Exception as error:
        return {'outcome': 'failed', 'error_type': type(error).__name__, 'steps': steps,
                '_response_completed_monotonic':response_clock}


def execute(schedule, operation, workers=4, max_dispatch_lag=.05, drain_timeout=30, start_callback=None):
    """Retain one record for every scheduled arrival, including unstarted work.

    No executor queue: a worker slot is reserved before submission. Slow targets
    produce admission drops; a late scheduler produces dispatch-lag drops.
    """
    if (type(workers) is not int or not 1 <= workers <= 128 or
            not math.isfinite(max_dispatch_lag) or max_dispatch_lag < 0 or
            not math.isfinite(drain_timeout) or drain_timeout <= 0 or
            (start_callback is not None and not callable(start_callback))):
        raise ValueError('invalid generator bounds')
    if any(item['at_s'] < 0 for item in schedule) or any(
            a['at_s'] > b['at_s'] for a, b in zip(schedule, schedule[1:])):
        raise ValueError('schedule must have nonnegative increasing arrival times')
    slots = threading.BoundedSemaphore(workers)
    start = time.perf_counter()
    records, futures, submitted = [], [], {}
    def work(item, intended):
        dispatched = time.perf_counter()-start
        try:
            result = operation(item)
            validated=time.perf_counter()
            response_completed=result.pop('_response_completed_monotonic',None) or validated
            return {**item, **result, 'intended_s': intended, 'dispatch_s': dispatched,
                    'complete_s': response_completed-start,'validation_complete_s':validated-start}
        except Exception as error:
            return {**item, 'outcome': 'failed', 'error_type': type(error).__name__,
                    'intended_s': intended, 'dispatch_s': dispatched,
                    'complete_s': time.perf_counter()-start}
        finally:
            slots.release()
    executor = ThreadPoolExecutor(max_workers=workers)
    try:
        if start_callback is not None:start_callback(start)
        for item in schedule:
            intended = item['at_s']
            remaining = start+intended-time.perf_counter()
            if remaining > 0:
                time.sleep(remaining)
            now = time.perf_counter()-start
            if now-intended > max_dispatch_lag:
                records.append({**item, 'intended_s': intended, 'observed_s': now,
                                'outcome': 'dispatch_lag_drop'})
            elif not slots.acquire(blocking=False):
                records.append({**item, 'intended_s': intended, 'observed_s': now,
                                'outcome': 'admission_drop'})
            else:
                future = executor.submit(work, item, intended)
                futures.append(future)
                submitted[future] = item
        deadline = start + (schedule[-1]['at_s'] if schedule else 0) + drain_timeout
        done, pending = wait(futures, timeout=max(0, deadline-time.perf_counter()))
        records.extend(f.result() for f in done)
        for future in pending:
            future.cancel()
            item = submitted[future]
            records.append({**item, 'intended_s': item['at_s'], 'observed_s': time.perf_counter()-start,
                            'outcome': 'drain_timeout'})
    finally:
        # Python threads cannot be force-killed. Callers must stop a campaign on
        # drain_timeout and terminate its service processes before another cell.
        executor.shutdown(wait=False, cancel_futures=True)
    return sorted(records, key=lambda record: record['id'])
