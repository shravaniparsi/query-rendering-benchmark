"""SSR/CSR front end for synthetic reserved-task correctness checks."""
import json
import os
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, ProxyHandler
from flask import Flask, request, render_template, jsonify, g

app = Flask(__name__); app.config['MAX_CONTENT_LENGTH'] = 32768
ORIGIN = os.environ['RESERVED_ORIGIN']; TASK = os.environ['RESERVED_TASK']; TRANSPORT = build_opener(ProxyHandler({}))
if TASK not in ('inventory', 'analytics') or not ORIGIN.startswith('http://127.0.0.1:'): raise ValueError('reserved loopback origin required')
DEFAULT = {} if TASK == 'inventory' else {'start_s': 100, 'end_s': 200}


@app.template_filter('money')
def money(value):
    value = int(value); magnitude = abs(value)
    return ('-' if value < 0 else '') + '$' + str(magnitude // 100) + '.' + str(magnitude % 100).zfill(2)


def operation_id():
    if not hasattr(g, 'operation_id'):
        value = request.headers.get('X-QRL-Operation') or request.args.get('operation_id') or uuid.uuid4().hex
        if not isinstance(value, str) or not 0 < len(value) <= 128: raise ValueError('bounded request identity required')
        g.operation_id = value
    return g.operation_id


def origin(path, payload=None):
    operation = operation_id()
    if payload is not None: operation = payload.get('operation_id', payload.get('event_id', operation))
    if not isinstance(operation, str) or not 0 < len(operation) <= 128: raise ValueError('bounded operation identity required')
    headers = {'Cache-Control': 'no-store', 'X-QRL-Operation': operation}
    if payload is not None: headers.update({'Content-Type': 'application/json', 'X-QRL-Control': 'engineering'})
    req = Request(ORIGIN + path, data=None if payload is None else json.dumps(payload).encode(), headers=headers)
    try:
        with TRANSPORT.open(req, timeout=15) as response: return response.status, json.load(response)
    except HTTPError as error:
        return error.code, {'ok': False, 'error': {'code': 'origin_http_failure', 'upstream_status': error.code}}
    except (URLError, TimeoutError): return 502, {'ok': False, 'error': {'code': 'origin_transport_failure', 'upstream_status': 502}}


def parameters():
    value = json.loads(request.args.get('parameters', json.dumps(DEFAULT)))
    if not isinstance(value, dict): raise ValueError('object parameters required')
    return value


@app.after_request
def no_cache(response): response.headers['Cache-Control'] = 'no-store'; return response


@app.get('/health')
def health(): return jsonify(ok=True, task=TASK)


@app.get('/task/<strategy>')
def page(strategy):
    if strategy not in ('ssr', 'csr'): return jsonify(ok=False), 404
    try:
        args = parameters(); identity = operation_id()
        if len(identity) > 80: raise ValueError('page identity leaves no action suffix budget')
        result = origin('/query?' + urlencode({'parameters': json.dumps(args)}))[1] if strategy == 'ssr' else None
        return render_template('dashboard.html', task=TASK, strategy=strategy, parameters=args, result=result,
            operation_prefix=identity)
    except (ValueError, TypeError): return jsonify(ok=False, error='invalid_request'), 400


@app.get('/api/task')
def api():
    try: status, result = origin('/query?' + urlencode({'parameters': json.dumps(parameters())})); return jsonify(result), status
    except (ValueError, TypeError): return jsonify(ok=False, error='invalid_request'), 400


@app.post('/api/task/mutate')
def mutate():
    expected = os.environ.get('RESERVED_PUBLIC_ORIGIN', request.host_url.rstrip('/'))
    if request.headers.get('Origin') != expected or not request.is_json: return jsonify(ok=False, error='same_origin_json_required'), 403
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict): return jsonify(ok=False, error='invalid_request'), 400
    try: status, result = origin('/control/mutate', payload); return jsonify(result), status
    except (ValueError, TypeError): return jsonify(ok=False, error='invalid_request'), 400
