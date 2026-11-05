from copy import deepcopy
import os
import unittest
import uuid

from experiments.heldout_tasks.scale_sql import ScaleInventorySQL, ScaleAnalyticsSQL
from experiments.heldout_tasks.scale_reference import InventoryScaleReference, AnalyticsScaleReference
from experiments.heldout_tasks.inventory import InventoryOracle
from experiments.heldout_tasks.analytics import AnalyticsOracle


@unittest.skipUnless(os.environ.get('QRL_TEST_DATABASE_URL'), 'disposable PostgreSQL required')
class ReservedScaleContracts(unittest.TestCase):
    def fixture(self, cls, rows, kind):
        value = cls(os.environ['QRL_TEST_DATABASE_URL'], 'qrl_reserved_' + uuid.uuid4().hex); self.addCleanup(value.drop)
        value.initialize_scale(rows, kind); self.assertEqual(value.configuration()['query_kind'], kind); return value

    def test_inventory_scale_all_queries_and_original_oracle_agree(self):
        for rows in (100, 1000):
            for kind in ('correlated', 'batched', 'maintained'):
                with self.subTest(rows=rows, kind=kind):
                    fixture = self.fixture(ScaleInventorySQL, rows, kind); probes = [fixture.query()]
                    requests = [('new-1', 'reserve', 'new-r1', {'product_id': 1, 'quantity': 1}),
                        ('cancel-seed', 'cancel', 'seed-reservation-5', {}), ('duplicate-cancel', 'cancel', 'seed-reservation-5', {}),
                        ('cancel-new', 'cancel', 'new-r1', {}), ('missing', 'cancel', 'absent', {}),
                        ('insufficient', 'reserve', 'cannot-reserve', {'product_id': 5, 'quantity': 100}),
                        ('exists', 'reserve', 'seed-reservation-1', {'product_id': 1, 'quantity': 1})]
                    for key, operation, reservation, payload in requests:
                        fixture.apply(key, operation, reservation, **payload); probes.append(fixture.query())
                    probes.extend(fixture.query(**args) for args in ({'available_only': False}, {'category': 'category-1', 'limit': 3, 'offset': 2}, {'brand': 'absent'}, {'min_price_minor': 200, 'max_price_minor': 1500}))
                    state = fixture.validation_state(); reference = InventoryScaleReference(state); self.assertEqual(reference.verify_final()['seed_products_checked'], rows)
                    for probe in probes: self.assertTrue(reference.verify_query(probe))
                    if rows == 100:
                        with fixture.connection() as connection:
                            products = [dict(zip(('id', 'category', 'brand', 'price_minor', 'initial_stock'), row)) for row in connection.execute('SELECT * FROM products ORDER BY id')]
                        for probe in probes:
                            original = InventoryOracle(products)
                            for i in range(1, rows + 1): original.apply('seed-reserve-' + str(i), 'reserve', 'seed-reservation-' + str(i), product_id=i, quantity=1)
                            for i in reference.active(probe['snapshot']):
                                row = state['operations'][i]; self.assertEqual(original.apply(row['operation_id'], **row['request']), row['result'])
                            expected = original.query(**probe['parameters']); self.assertTrue(all(probe[k] == value for k, value in expected.items()))
                    with self.assertRaises(ValueError): fixture.apply('seed-forbidden', 'cancel', 'new-r1')

    def test_analytics_scale_all_queries_and_original_oracle_agree(self):
        for rows in (100, 1000):
            for kind in ('correlated', 'batched', 'maintained'):
                with self.subTest(rows=rows, kind=kind):
                    fixture = self.fixture(ScaleAnalyticsSQL, rows, kind); probes = [fixture.query(100, 200)]
                    requests = [dict(event_id='refund-seed', kind='refund', event_time_s=110, amount_minor=100, sale_id='seed-sale-0'),
                        dict(event_id='late', kind='sale', event_time_s=99, amount_minor=2000, region='R', category='C'),
                        dict(event_id='refund-late', kind='refund', event_time_s=150, amount_minor=100, sale_id='late'),
                        dict(event_id='too-early', kind='refund', event_time_s=100, amount_minor=1, sale_id='seed-sale-1'),
                        dict(event_id='too-large', kind='refund', event_time_s=110, amount_minor=10000, sale_id='seed-sale-0'),
                        dict(event_id='absent', kind='refund', event_time_s=110, amount_minor=1, sale_id='missing')]
                    for payload in requests: fixture.append(**payload); probes.append(fixture.query(100, 200))
                    probes.extend(fixture.query(a, b, **kwargs) for a, b, kwargs in ((99, 100, {}), (110, 111, {}), (1, 2, {}), (99, 3700, {'group_by': ('region',)}), (100, 300, {'category': 'category-1', 'group_by': ('category',), 'limit': 1, 'offset': 1})))
                    state = fixture.validation_state(); reference = AnalyticsScaleReference(state); self.assertEqual(reference.verify_final()['seed_events_checked'], rows)
                    with fixture.connection() as connection: seed = [row[0] for row in connection.execute("SELECT request FROM operations WHERE event_id LIKE 'seed-sale-%' ORDER BY operation_sequence")]
                    for probe in probes:
                        self.assertTrue(reference.verify_query(probe)); original = AnalyticsOracle()
                        for request in seed: original.append(**request)
                        for i in reference.active(probe['snapshot']):
                            row = state['operations'][i]; self.assertEqual(original.append(**row['request']), row['result'])
                        args = {**probe['parameters'], 'group_by': tuple(probe['parameters']['group_by'])}; expected = original.query(**args)
                        self.assertTrue(all(probe[k] == value for k, value in expected.items()))
                    with self.assertRaises(ValueError): fixture.append('seed-forbidden', 'sale', 100, 1, region='R', category='C')

    def test_native_digest_snapshot_and_result_corruptions_rejected(self):
        fixture = self.fixture(ScaleInventorySQL, 100, 'maintained'); fixture.apply('reserve', 'reserve', 'new', product_id=1, quantity=1)
        probe = fixture.query(); state = fixture.validation_state(); bad = deepcopy(state); bad['observed_products']['sha256'] = '0' * 64
        with self.assertRaises(ValueError): InventoryScaleReference(bad)
        bad = deepcopy(state); bad['observed_balances']['rows'] -= 1
        with self.assertRaises(ValueError): InventoryScaleReference(bad).verify_final()
        bad = deepcopy(state); bad['operations'][0]['result']['outcome'] = 'insufficient_stock'
        with self.assertRaises(ValueError): InventoryScaleReference(bad).verify_final()
        bad = deepcopy(probe); bad['snapshot'] = '0:1:'
        with self.assertRaises(ValueError): InventoryScaleReference(state).verify_query(bad)
        fixture = self.fixture(ScaleAnalyticsSQL, 100, 'maintained'); probe = fixture.query(100, 200); state = fixture.validation_state(); bad = deepcopy(state); bad['observed_events']['sha256'] = '0' * 64
        with self.assertRaises(ValueError): AnalyticsScaleReference(bad).verify_final()
        bad = deepcopy(probe); bad['groups'][0]['net_total_minor'] += 1
        with self.assertRaises(ValueError): AnalyticsScaleReference(state).verify_query(bad)


if __name__ == '__main__': unittest.main()
