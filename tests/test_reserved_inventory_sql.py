from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import os
import threading
import unittest
import uuid

from experiments.heldout_tasks.inventory_sql import InventorySQL, verify_final, verify_query, reconstruct


@unittest.skipUnless(os.environ.get('QRL_TEST_DATABASE_URL'), 'disposable PostgreSQL required')
class InventorySQLContracts(unittest.TestCase):
    def fixture(self, kind):
        value = InventorySQL(os.environ['QRL_TEST_DATABASE_URL'], 'qrl_reserved_' + uuid.uuid4().hex)
        self.addCleanup(value.drop)
        products = [{'id': i, 'category': 'category-' + str(i % 4), 'brand': 'brand-' + str(i % 3),
            'price_minor': 100 + i * 37, 'initial_stock': 3 if i == 1 else i % 5} for i in range(1, 31)]
        value.initialize(products, kind); return value

    def test_all_query_forms_preserve_exact_rows_count_and_conjunctive_facets(self):
        for kind in ('correlated', 'batched', 'maintained'):
            with self.subTest(kind=kind):
                fixture = self.fixture(kind); self.assertEqual(fixture.configuration()['query_kind'], kind)
                probes = [fixture.query(**args) for args in ({}, {'available_only': False}, {'category': 'category-1'},
                    {'brand': 'brand-2', 'min_price_minor': 200, 'max_price_minor': 800, 'limit': 3, 'offset': 2},
                    {'category': 'absent'}, {'offset': 100}, {'limit': 1, 'available_only': False})]
                state = fixture.validation_state(); self.assertEqual(verify_final(state)['operations'], 0)
                for probe in probes:
                    self.assertTrue(verify_query(state, probe))
                    self.assertTrue(all(type(row[k]) is int for row in probe['items'] for k in ('id', 'price_minor', 'initial_stock', 'remaining_stock')))

    def test_competing_reservations_no_oversell_cancel_and_idempotency(self):
        for kind in ('correlated', 'batched', 'maintained'):
            with self.subTest(kind=kind):
                fixture = self.fixture(kind); barrier = threading.Barrier(8)
                def reserve(index):
                    barrier.wait(); return fixture.apply(f'op-{index}', 'reserve', f'reservation-{index}', product_id=1, quantity=1)
                with ThreadPoolExecutor(max_workers=8) as pool: results = list(pool.map(reserve, range(8)))
                self.assertEqual(sum(r['outcome'] == 'reserved' for r in results), 3)
                self.assertEqual(sum(r['outcome'] == 'insufficient_stock' for r in results), 5)
                winner = next(i for i, r in enumerate(results) if r['outcome'] == 'reserved')
                self.assertEqual(fixture.apply(f'op-{winner}', 'reserve', f'reservation-{winner}', product_id=1, quantity=1), results[winner])
                with self.assertRaises(ValueError): fixture.apply(f'op-{winner}', 'reserve', f'reservation-{winner}', product_id=1, quantity=2)
                self.assertEqual(fixture.apply('cancel', 'cancel', f'reservation-{winner}')['outcome'], 'cancelled')
                self.assertEqual(fixture.apply('cancel-again', 'cancel', f'reservation-{winner}')['outcome'], 'already_cancelled')
                self.assertEqual(fixture.apply('missing', 'cancel', 'does-not-exist')['outcome'], 'not_found')
                self.assertEqual(fixture.apply('replace', 'reserve', 'replacement', product_id=1, quantity=1)['outcome'], 'reserved')
                probe = fixture.query(available_only=False); state = fixture.validation_state(); verify_final(state); verify_query(state, probe)
                self.assertEqual(reconstruct(state).state()['products'][0]['remaining_stock'], 0)
                self.assertEqual(len(state['operations']), 12)

    def test_concurrent_idempotency_has_one_committed_effect(self):
        fixture = self.fixture('maintained'); barrier = threading.Barrier(6)
        def reserve(_): barrier.wait(); return fixture.apply('same-operation', 'reserve', 'same-reservation', product_id=1, quantity=2)
        with ThreadPoolExecutor(max_workers=6) as pool: values = list(pool.map(reserve, range(6)))
        self.assertTrue(all(value == values[0] for value in values)); state = fixture.validation_state()
        self.assertEqual(verify_final(state)['operations'], 1); self.assertEqual(state['balances'][0]['remaining_stock'], 1)

    def test_snapshots_during_mutations_and_corruptions(self):
        fixture = self.fixture('maintained'); started = threading.Event(); probes = []
        def writer():
            started.set()
            for i in range(15):
                fixture.apply(f'reserve-{i}', 'reserve', f'item-{i}', product_id=1, quantity=1)
                fixture.apply(f'cancel-{i}', 'cancel', f'item-{i}')
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(writer); started.wait()
            for _ in range(20): probes.append(fixture.query(available_only=False, limit=3))
            future.result()
        state = fixture.validation_state(); verify_final(state)
        for probe in probes: verify_query(state, probe)
        bad = deepcopy(probes[-1]); bad['itemsCount'] += 1
        with self.assertRaises(ValueError): verify_query(state, bad)
        bad = deepcopy(state); bad['balances'][0]['remaining_stock'] = -1
        with self.assertRaises(ValueError): verify_final(bad)
        bad = deepcopy(state); bad['operations'][0]['result']['outcome'] = 'insufficient_stock'
        with self.assertRaises(ValueError): verify_final(bad)

    def test_invalid_payloads_do_not_commit_partial_mutations(self):
        fixture = self.fixture('maintained')
        for kwargs in ({'product_id': 999, 'quantity': 1}, {'product_id': 1, 'quantity': 0}, {'product_id': True, 'quantity': 1}):
            with self.assertRaises(ValueError): fixture.apply('invalid', 'reserve', 'invalid-reservation', **kwargs)
        with self.assertRaises(ValueError): fixture.query(min_price_minor=300, max_price_minor=100)
        with self.assertRaises(ValueError): fixture.query(available_only=1)
        self.assertEqual(verify_final(fixture.validation_state())['operations'], 0)


if __name__ == '__main__': unittest.main()
