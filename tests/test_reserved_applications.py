import json
import os
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from experiments.heldout_tasks.runtime import reserved_runtime
from experiments.heldout_tasks import inventory_sql, analytics_sql


@unittest.skipUnless(os.environ.get('QRL_TEST_DATABASE_URL'), 'disposable PostgreSQL required')
class ReservedApplicationContracts(unittest.TestCase):
    def test_independent_origins_ssr_csr_faults_actions_and_request_guards(self):
        for task in ('inventory', 'analytics'):
            for kind in ('correlated', 'batched', 'maintained'):
                with self.subTest(task=task, kind=kind), tempfile.TemporaryDirectory() as temp:
                    with reserved_runtime(os.environ['QRL_TEST_DATABASE_URL'], Path(temp), task, kind) as runtime:
                        self.assertNotEqual(runtime['origin'].pid, os.getpid()); base = runtime['url']; probes = []
                        for strategy in ('ssr', 'csr'):
                            with urlopen(base + '/task/' + strategy) as response: html = response.read().decode()
                            self.assertIn('data-strategy="' + strategy + '"', html)
                            self.assertEqual('<article' in html, strategy == 'ssr')
                            with urlopen(base + '/api/task') as response: probes.append(json.load(response))
                        runtime['origin'].command('fault', 503)
                        with self.assertRaises(HTTPError) as raised: urlopen(base + '/api/task')
                        self.assertEqual(raised.exception.code, 503)
                        with urlopen(base + '/task/ssr') as response: self.assertIn('data-qrl-outcome="failed"', response.read().decode())
                        runtime['origin'].command('fault', None)
                        payload = {'operation_id': 'http-reserve', 'kind': 'reserve', 'reservation_id': 'http-reservation', 'product_id': 1, 'quantity': 1} if task == 'inventory' else {
                            'event_id': 'http-sale', 'kind': 'sale', 'event_time_s': 100, 'amount_minor': 100, 'region': 'region-0', 'category': 'category-0'}
                        with self.assertRaises(HTTPError) as raised:
                            urlopen(Request(base + '/api/task/mutate', data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'}))
                        self.assertEqual(raised.exception.code, 403)
                        request = Request(base + '/api/task/mutate', data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json', 'Origin': base})
                        with urlopen(request) as response: result = json.load(response)
                        self.assertTrue(result['ok'] and result['changed'])
                        with urlopen(request) as response: self.assertEqual(json.load(response), result)
                        with urlopen(base + '/api/task') as response: probes.append(json.load(response))
                        runtime['quiesce'](); state = runtime['fixture'].validation_state()
                        module = inventory_sql if task == 'inventory' else analytics_sql; module.verify_final(state)
                        for probe in probes: self.assertTrue(module.verify_query(state, probe))
                        events = [json.loads(line) for line in (Path(temp) / 'backend.jsonl').read_text().splitlines()]
                        self.assertTrue(events and all(e['comparative_timing'] is False for e in events))
                        self.assertTrue(all(not any('time' in key or 'latency' in key for key in e if key != 'comparative_timing') for e in events))

    def test_performance_cannot_enter_correctness_runtime(self):
        with self.assertRaises(ValueError), tempfile.TemporaryDirectory() as temp:
            with reserved_runtime(os.environ['QRL_TEST_DATABASE_URL'], Path(temp), 'inventory', 'maintained', purpose='performance'): pass


if __name__ == '__main__': unittest.main()
