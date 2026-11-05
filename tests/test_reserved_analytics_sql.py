from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import os
import threading
import unittest
import uuid

from experiments.heldout_tasks.analytics_sql import AnalyticsSQL, verify_final, verify_query


@unittest.skipUnless(os.environ.get('QRL_TEST_DATABASE_URL'), 'disposable PostgreSQL required')
class AnalyticsSQLContracts(unittest.TestCase):
    def fixture(self, kind):
        value = AnalyticsSQL(os.environ['QRL_TEST_DATABASE_URL'], 'qrl_reserved_' + uuid.uuid4().hex)
        self.addCleanup(value.drop); value.initialize(kind); return value

    def seed(self, fixture):
        for i in range(12):
            fixture.append('sale-' + str(i), 'sale', 100 + i, 1000 + i, region='region-' + str(i % 3), category='category-' + str(i % 2))
        fixture.append('late-sale', 'sale', 99, 2000, region='region-1', category='category-0')
        fixture.append('refund', 'refund', 110, 101, sale_id='sale-0')
        fixture.append('refund-outside', 'refund', 200, 102, sale_id='sale-0')

    def test_all_queries_late_events_half_open_windows_grouping_and_pagination(self):
        for kind in ('correlated', 'batched', 'maintained'):
            with self.subTest(kind=kind):
                fixture = self.fixture(kind); self.seed(fixture); self.assertEqual(fixture.configuration()['query_kind'], kind)
                probes = [fixture.query(a, b, **kwargs) for a, b, kwargs in (
                    (99, 112, {}), (100, 110, {}), (110, 111, {}), (200, 201, {}), (1, 2, {}),
                    (99, 201, {'group_by': ('region',), 'limit': 2, 'offset': 1}),
                    (99, 201, {'group_by': ('category',), 'region': 'region-1'}),
                    (99, 201, {'category': 'absent'}), (99, 201, {'offset': 100}))]
                state = fixture.validation_state(); self.assertEqual(verify_final(state)['events'], 15)
                for probe in probes: self.assertTrue(verify_query(state, probe))
                self.assertEqual(probes[1]['eventCount'], 10)
                self.assertEqual(probes[3]['groups'][0]['net_total_minor'], -102)

    def test_concurrent_refunds_cannot_exceed_sale_and_replays_are_idempotent(self):
        for kind in ('correlated', 'batched', 'maintained'):
            with self.subTest(kind=kind):
                fixture = self.fixture(kind); fixture.append('sale', 'sale', 100, 1000, region='R', category='C'); barrier = threading.Barrier(8)
                def refund(i):
                    if i < 8: barrier.wait()
                    return fixture.append('refund-' + str(i), 'refund', 110, 100, sale_id='sale')
                with ThreadPoolExecutor(max_workers=8) as pool: results = list(pool.map(refund, range(16)))
                self.assertEqual(sum(r['changed'] for r in results), 10)
                self.assertEqual(sum(r['outcome'] == 'refund_exceeds_sale' for r in results), 6)
                index = next(i for i, r in enumerate(results) if r['changed'])
                self.assertEqual(fixture.append('refund-' + str(index), 'refund', 110, 100, sale_id='sale'), results[index])
                with self.assertRaises(ValueError): fixture.append('refund-' + str(index), 'refund', 110, 101, sale_id='sale')
                probe = fixture.query(100, 120); state = fixture.validation_state(); verify_final(state); verify_query(state, probe)
                self.assertEqual(probe['groups'][0]['net_total_minor'], 0); self.assertEqual(len(state['operations']), 17)

    def test_absent_sale_and_refund_time_validation_are_persisted_outcomes(self):
        fixture = self.fixture('maintained')
        self.assertEqual(fixture.append('missing-refund', 'refund', 100, 50, sale_id='sale')['outcome'], 'sale_not_found')
        fixture.append('sale', 'sale', 101, 100, region='R', category='C')
        self.assertEqual(fixture.append('early', 'refund', 100, 50, sale_id='sale')['outcome'], 'refund_precedes_sale')
        self.assertEqual(fixture.append('missing-refund', 'refund', 100, 50, sale_id='sale')['outcome'], 'sale_not_found')
        state = fixture.validation_state(); self.assertEqual(verify_final(state)['events'], 1)

    def test_concurrent_duplicate_sale_has_one_effect(self):
        fixture = self.fixture('maintained'); barrier = threading.Barrier(6)
        def sale(_): barrier.wait(); return fixture.append('same', 'sale', 100, 123, region='R', category='C')
        with ThreadPoolExecutor(max_workers=6) as pool: values = list(pool.map(sale, range(6)))
        self.assertTrue(all(v == values[0] for v in values)); state = fixture.validation_state(); self.assertEqual(verify_final(state)['events'], 1)

    def test_snapshot_consistency_under_late_ingestion_and_negative_controls(self):
        fixture = self.fixture('maintained'); self.seed(fixture); started = threading.Event(); probes = []
        def writer():
            started.set()
            for i in range(20): fixture.append(f'late-{i}', 'sale', 100 + i % 3, 100 + i, region='late', category='C')
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(writer); started.wait()
            for _ in range(20): probes.append(fixture.query(100, 110, group_by=('region',)))
            future.result()
        state = fixture.validation_state(); verify_final(state)
        for probe in probes: verify_query(state, probe)
        bad = deepcopy(probes[-1]); bad['groups'][0]['net_total_minor'] += 1
        with self.assertRaises(ValueError): verify_query(state, bad)
        bad = deepcopy(state); bad['buckets'][0]['gross_total_minor'] += 1
        with self.assertRaises(ValueError): verify_final(bad)
        bad = deepcopy(state); bad['raw_events'][0]['event_time_s'] += 1
        with self.assertRaises(ValueError): verify_final(bad)

    def test_invalid_inputs_leave_no_partial_events(self):
        fixture = self.fixture('maintained')
        with self.assertRaises(ValueError): fixture.append('invalid', 'sale', True, 100, region='R', category='C')
        with self.assertRaises(ValueError): fixture.append('invalid', 'sale', 100, 0, region='R', category='C')
        with self.assertRaises(ValueError): fixture.query(10, 10)
        with self.assertRaises(ValueError): fixture.query(1, 10, group_by=('event_id',))
        with fixture.connection() as connection:
            with self.assertRaises(fixture.driver.errors.CheckViolation): connection.execute("INSERT INTO event_buckets VALUES (100,'R','C',0.5,0,1,0)")
        self.assertEqual(verify_final(fixture.validation_state())['events'], 0)


if __name__ == '__main__': unittest.main()
