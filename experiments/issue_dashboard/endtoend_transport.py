"""Owned HTTP/1.1 logical work with a body-receipt clock before validation."""
import hashlib
import http.client
import json
import socket
import time
from urllib.parse import urlencode, urlsplit

from .arrivals import IssueHTML
from .persistent_http import OwnedSession, cpus

POLICY = 'owned-http11-receipt-v2'


class ContractError(ValueError): pass


class ReceiptSession(OwnedSession):
    def request(self, method, path, *, operation_id, body=None):
        self.check_owner(); parsed = urlsplit(path)
        if method not in ('GET', 'POST') or parsed.scheme or parsed.netloc or not path.startswith('/') or len(path) > 2048 or parsed.fragment:
            raise ContractError('bounded relative GET/POST required')
        if not isinstance(operation_id, str) or not 0 < len(operation_id) <= 128: raise ContractError('operation identity required')
        if body is not None and (not isinstance(body, bytes) or len(body) > 32768): raise ContractError('bounded byte body required')
        if method == 'GET' and body is not None: raise ContractError('GET body forbidden')
        if self.connection and (not self.persistent or self.used >= self.max_requests or time.monotonic() - self.last >= self.idle_s): self.close()
        if self.connection is None: self.connection = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout_s)
        connection = self.connection
        row = {'policy': POLICY, 'operation_id': operation_id, 'method': method, 'path': path,
            'persistent': self.persistent, 'outcome': 'failed', 'error_type': None, 'allowed_cpus': cpus(),
            'status': None, 'server_connection_id': None, 'body_bytes': None, 'body_sha256': None,
            'body_receipt_ns': None, 'validation_finish_ns': None, 'client_address': None,
            'peer_address': None, 'http_version': None, 'will_close': None, 'attempts': 1}
        try:
            headers = {'Cache-Control': 'no-store', 'X-QRL-Operation': operation_id,
                'Connection': 'keep-alive' if self.persistent else 'close'}
            if body is not None: headers.update({'Content-Type': 'application/json', 'X-QRL-Control': 'engineering'})
            connection.request(method, path, body=body, headers=headers)
            row.update(client_address=list(connection.sock.getsockname()), peer_address=list(connection.sock.getpeername()))
            response = connection.getresponse(); raw = response.read(4 * 1024 * 1024 + 1)
            row['body_receipt_ns'] = str(time.perf_counter_ns())
            if len(raw) > 4 * 1024 * 1024: raise ContractError('response bound exceeded')
            row.update(status=response.status, server_connection_id=response.getheader('X-QRL-Connection'),
                http_version=response.version, will_close=response.will_close,
                body_bytes=len(raw), body_sha256=hashlib.sha256(raw).hexdigest())
            if response.version != 11 or not row['server_connection_id']: raise ContractError('observed HTTP/1.1 gateway identity required')
            row['outcome'] = 'completed'; self.used += 1; self.last = time.monotonic()
            if response.will_close or not self.persistent: self.close()
            return row, raw
        except (OSError, http.client.HTTPException) as error:
            row.update(outcome='failed', error_type=type(error).__name__); self.close(); return row, None
        except Exception as error:
            row.update(outcome='invalid', error_type=type(error).__name__); self.close(); raise
        finally:
            row['validation_finish_ns'] = str(time.perf_counter_ns()); self.records.append(row)


class IssueClient:
    def __init__(self, session, strategy, prefix, read_operation='discussion_rank'):
        if strategy not in ('ssr', 'csr') or read_operation != 'discussion_rank': raise ContractError('unknown endpoint contract')
        self.session, self.strategy, self.prefix, self.read_operation = session, strategy, prefix, read_operation

    def invoke(self, item):
        steps = []; received = None; evidence = ack = None
        operation_id = f'{self.prefix}-{item["id"]}'
        def send(path, payload=None):
            nonlocal received
            row, raw = self.session.request('GET' if payload is None else 'POST', path,
                operation_id=operation_id, body=None if payload is None else json.dumps(payload).encode())
            if row['body_receipt_ns'] is not None: received = int(row['body_receipt_ns']) / 1e9
            steps.append({'status': row['status'], 'bytes': row['body_bytes'], 'path': path,
                'server_connection_id': row['server_connection_id'], 'outcome': row['outcome']})
            return row, raw
        def failure(code, status=None):
            return {'outcome': 'failed', 'failure_code': code, 'upstream_status': status, 'steps': steps,
                'read_evidence': None, 'acknowledgement': None, '_response_completed_monotonic': received}
        try:
            if item['kind'] == 'write':
                row, raw = send('/control/mutate', {'operation_id': operation_id, 'issue_id': item['issue_id'],
                    'kind': item['mutation'], 'value': item['value']})
                if row['outcome'] != 'completed': return failure('network_failure')
                if row['status'] != 200: return failure('write_http_failure', row['status'])
                ack = json.loads(raw)
                if not (ack.get('changed') is True and ack.get('operation_id') == operation_id and type(ack.get('revision')) is int and ack['revision'] >= 1):
                    raise ContractError('write acknowledgement differs')
            else:
                params = {k: item[k] for k in ('project_id', 'state', 'limit', 'offset')}; query = urlencode(params)
                row, raw = send('/issues/' + self.strategy + '?' + query)
                if row['outcome'] != 'completed': return failure('network_failure')
                if row['status'] != 200: return failure('page_http_failure', row['status'])
                parser = IssueHTML(); parser.feed(raw.decode());
                if len(parser.main) != 1: raise ContractError('single application main required')
                main = parser.main[0]
                if self.strategy == 'csr':
                    if parser.rows or main['data-qrl-outcome'] != 'loading': raise ContractError('CSR shell differs')
                    row, raw = send('/api/issues?' + query)
                    if row['outcome'] != 'completed': return failure('network_failure')
                    result = json.loads(raw)
                    if row['status'] != 200 or not result.get('ok'):
                        error = result.get('error', {}); return failure(error.get('code', 'api_http_failure'), error.get('upstream_status', row['status']))
                    if result.get('read_operation') != self.read_operation: raise ContractError('API operation identity differs')
                    evidence = {k: result[k] for k in ('snapshot', 'issues', 'issuesCount', 'read_operation')}
                else:
                    if main['data-qrl-outcome'] == 'failed': return failure(main['data-qrl-error'], int(main['data-upstream-status']))
                    if main['data-qrl-outcome'] != 'completed' or main.get('data-response-operation') != self.read_operation:
                        raise ContractError('SSR operation identity differs')
                    evidence = {'snapshot': main['data-snapshot'], 'issues': parser.rows, 'issuesCount': int(parser.count), 'read_operation': self.read_operation}
                evidence.update(params)
            return {'outcome': 'completed', 'steps': steps, 'read_evidence': evidence, 'acknowledgement': ack,
                '_response_completed_monotonic': received}
        except (ValueError, KeyError, TypeError, UnicodeError) as error:
            return {'outcome': 'integrity_failure', 'error_type': type(error).__name__, 'steps': steps,
                'read_evidence': evidence, 'acknowledgement': ack, '_response_completed_monotonic': received}
