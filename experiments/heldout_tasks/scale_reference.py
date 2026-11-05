"""Independent scalable snapshot reconstruction from seed specifications/ledgers.

This module imports no SQL fixture, service, or query implementation. Native
whole-table digests cover deterministic initial/final rows; mutable operations
are replayed with Python domain rules before any page or aggregate is checked.
"""
from collections import Counter, defaultdict
import hashlib
import heapq
import itertools
import json

from .inventory import integer, require
from experiments.query_rendering_load.snapshot_oracle import parse_snapshot


def digest(rows):
    output = hashlib.sha256(); count = 0
    for row in rows:
        encoded = json.dumps(row, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
        output.update(encoded.encode('ascii') + b'\n'); count += 1
    return {'rows': count, 'sha256': output.hexdigest()}


class SeedReference:
    def initialize(self, state, task, identity):
        require(state['format'] == identity and state['seed'] == {'version': 'reserved-scale-seed-v1', 'task': task, 'rows': state['seed']['rows']} and
            type(state['seed']['rows']) is int and 100 <= state['seed']['rows'] <= 100000 and state['query_kind'] in ('correlated', 'batched', 'maintained'), 'scale seed/state identity differs')
        require(type(state['seed_xid']) is int and state['seed_xid'] > 0, 'native initial transaction required')
        self.state = state; self.rows = state['seed']['rows']; self.seed_xid = state['seed_xid']; self.operations = state['operations']; self.cache = {}
        name = 'operation_id' if task == 'inventory' else 'event_id'; seen = set(); last = self.rows
        for row in self.operations:
            key = row[name]; require(isinstance(key, str) and 0 < len(key) <= 128 and not key.startswith('seed-') and key not in seen and
                type(row['operation_sequence']) is int and row['operation_sequence'] > last and type(row['transaction_xid']) is int and row['transaction_xid'] > self.seed_xid, 'mutable operation identity/order differs')
            seen.add(key); last = row['operation_sequence']

    def active(self, snapshot):
        if snapshot is None: return tuple(range(len(self.operations)))
        xmin, xmax, active = parse_snapshot(snapshot)
        def visible(xid): return xid < xmin or xid < xmax and xid not in active
        require(visible(self.seed_xid), 'page does not see the complete committed seed')
        return tuple(index for index, row in enumerate(self.operations) if visible(row['transaction_xid']))


class InventoryScaleReference(SeedReference):
    def __init__(self, state):
        self.initialize(state, 'inventory', 'reserved-inventory-scale-state-v1'); self.filters = {}
        require(state['read_operation'] == 'inventory_page', 'inventory operation identity differs')
        require(state['observed_products'] == digest(self.product(i) for i in range(1, self.rows + 1)), 'native product seed differs')
        def seeded():
            for i in range(1, self.rows + 1):
                key, reservation = 'seed-reserve-' + str(i), 'seed-reservation-' + str(i)
                yield {'operation_id': key, 'request': {'kind': 'reserve', 'reservation_id': reservation, 'product_id': i, 'quantity': 1},
                    'result': {'operation_id': key, 'reservation_id': reservation, 'outcome': 'reserved', 'changed': True}, 'operation_sequence': i, 'transaction_xid': self.seed_xid}
        require(state['observed_seed_operations'] == digest(seeded()), 'native inventory seed operations differ')

    def product(self, i):
        return {'id': i, 'category': 'category-' + str(i % 4), 'brand': 'brand-' + str(i % 3), 'price_minor': 100 + 37 * i, 'initial_stock': i % 5 + 1}

    def seed_reservation(self, key):
        prefix = 'seed-reservation-'; suffix = key[len(prefix):] if key.startswith(prefix) else ''
        if suffix.isdigit() and str(int(suffix)) == suffix and 1 <= int(suffix) <= self.rows:
            return {'product_id': int(suffix), 'quantity': 1, 'status': 'active'}
        return None

    def reduce(self, snapshot=None):
        key = self.active(snapshot)
        if key in self.cache: return self.cache[key]
        reservations = {}; used = Counter()
        for index in key:
            operation = self.operations[index]; payload = operation['request']; opid = operation['operation_id']
            require(set(payload) == {'kind', 'reservation_id', 'product_id', 'quantity'} and payload['kind'] in ('reserve', 'cancel') and isinstance(payload['reservation_id'], str) and payload['reservation_id'], 'inventory ledger request differs')
            reservation_id = payload['reservation_id']; existing = reservations.get(reservation_id, self.seed_reservation(reservation_id)); product, quantity = payload['product_id'], payload['quantity']
            if payload['kind'] == 'reserve':
                require(integer(product, 1) and product <= self.rows and integer(quantity, 1), 'invalid inventory ledger reserve')
                if existing is not None: outcome = 'reservation_exists'
                elif product % 5 - used[product] < quantity: outcome = 'insufficient_stock'
                else:
                    reservations[reservation_id] = {'product_id': product, 'quantity': quantity, 'status': 'active'}; used[product] += quantity; outcome = 'reserved'
                    require(product % 5 - used[product] >= 0, 'replayed stock is negative')
            else:
                require(product is None and quantity is None, 'cancel ledger payload differs')
                if existing is None: outcome = 'not_found'
                elif existing['status'] == 'cancelled': outcome = 'already_cancelled'
                else:
                    reservations[reservation_id] = {**existing, 'status': 'cancelled'}; used[existing['product_id']] -= existing['quantity']; outcome = 'cancelled'
            expected = {'operation_id': opid, 'reservation_id': reservation_id, 'outcome': outcome, 'changed': outcome in ('reserved', 'cancelled')}
            require(operation['result'] == expected, 'SQL reservation outcome differs from independent replay')
        result = used, reservations; self.cache[key] = result; return result

    def verify_final(self):
        used, changed = self.reduce()
        def reservations():
            names = sorted(set(changed) | {'seed-reservation-' + str(i) for i in range(1, self.rows + 1)})
            for key in names: yield {'reservation_id': key, **changed.get(key, self.seed_reservation(key))}
        require(self.state['observed_reservations'] == digest(reservations()), 'native final reservations differ')
        expected = digest({'product_id': i, 'remaining_stock': i % 5 - used[i]} for i in range(1, self.rows + 1)) if self.state['query_kind'] == 'maintained' else None
        require(self.state['observed_balances'] == expected, 'native maintained stock differs')
        return {'status': 'passed', 'seed_products_checked': self.rows, 'seed_operations_checked': self.rows, 'mutable_operations': len(self.operations), 'stock_nonnegative': True}

    def expected(self, snapshot, *, category=None, brand=None, min_price_minor=0, max_price_minor=None, available_only=True, offset=0, limit=20):
        require(all(v is None or isinstance(v, str) for v in (category, brand)) and integer(min_price_minor) and
            (max_price_minor is None or integer(max_price_minor) and max_price_minor >= min_price_minor) and type(available_only) is bool and integer(offset) and integer(limit, 1) and limit <= 100, 'inventory page parameters differ')
        key = category, brand, min_price_minor, max_price_minor, available_only
        def matches(i):
            p = self.product(i)
            return (category is None or p['category'] == category) and (brand is None or p['brand'] == brand) and p['price_minor'] >= min_price_minor and (max_price_minor is None or p['price_minor'] <= max_price_minor)
        if key not in self.filters:
            ids = tuple(i for i in range(1, self.rows + 1) if matches(i) and (not available_only or i % 5 > 0))
            facets = {name: Counter(self.product(i)[name] for i in ids) for name in ('category', 'brand')}; self.filters[key] = ids, facets
        base_ids, base_facets = self.filters[key]; used, _ = self.reduce(snapshot); added = set(); removed = set()
        if available_only:
            for i in used:
                if not matches(i): continue
                before, after = i % 5 > 0, i % 5 - used[i] > 0
                if before and not after: removed.add(i)
                if after and not before: added.add(i)
        facets = {name: Counter(values) for name, values in base_facets.items()}
        for i, change in itertools.chain(((i, -1) for i in removed), ((i, 1) for i in added)):
            for name in facets: facets[name][self.product(i)[name]] += change
        sequence = heapq.merge((i for i in base_ids if i not in removed), sorted(added))
        items = [{**self.product(i), 'remaining_stock': i % 5 - used[i]} for i in itertools.islice(sequence, offset, offset + limit)]
        return {'items': items, 'itemsCount': len(base_ids) + len(added) - len(removed), 'facets': {name: dict(sorted((k, v) for k, v in values.items() if v)) for name, values in facets.items()}}

    def verify_query(self, evidence):
        require(evidence['read_operation'] == 'inventory_page', 'inventory page identity differs')
        expected = self.expected(evidence['snapshot'], **evidence['parameters'])
        require(all(type(row[k]) is int for row in evidence['items'] for k in ('id', 'price_minor', 'initial_stock', 'remaining_stock')) and all(evidence[k] == value for k, value in expected.items()), 'inventory snapshot rows/count/facets differ')
        return True


class AnalyticsScaleReference(SeedReference):
    metrics = ('gross_total_minor', 'refund_total_minor', 'sales_count', 'refund_count')
    def __init__(self, state):
        self.initialize(state, 'analytics', 'reserved-analytics-scale-state-v1'); self.windows = {}
        require(state['read_operation'] == 'event_time_groups', 'analytics operation identity differs')
        def seeded():
            for i in range(self.rows):
                event = self.sale(i)
                yield {'event_id': event['event_id'], 'request': event, 'result': {'event_id': event['event_id'], 'outcome': 'recorded', 'changed': True},
                    'operation_sequence': i + 1, 'transaction_xid': self.seed_xid}
        require(state['observed_seed_operations'] == digest(seeded()), 'native analytics seed operations differ')

    def sale(self, i):
        return {'event_id': 'seed-sale-' + str(i), 'kind': 'sale', 'event_time_s': 100 + i % 3600, 'amount_minor': 1000 + i % 101,
            'sale_id': None, 'region': 'region-' + str(i % 3), 'category': 'category-' + str(i % 4)}

    def seed_sale(self, key):
        prefix = 'seed-sale-'; suffix = key[len(prefix):] if key.startswith(prefix) else ''
        return self.sale(int(suffix)) if suffix.isdigit() and str(int(suffix)) == suffix and 0 <= int(suffix) < self.rows else None

    def reduce(self, snapshot=None):
        key = self.active(snapshot)
        if key in self.cache: return self.cache[key]
        events = []; sales = {}; refunded = Counter()
        for index in key:
            operation = self.operations[index]; request = operation['request']; event = dict(request); event_id = operation['event_id']
            require(set(request) == {'event_id', 'kind', 'event_time_s', 'amount_minor', 'sale_id', 'region', 'category'} and request['event_id'] == event_id and
                request['kind'] in ('sale', 'refund') and integer(request['event_time_s']) and integer(request['amount_minor'], 1), 'analytics ledger request differs')
            outcome = 'recorded'
            if request['kind'] == 'sale':
                require(request['sale_id'] is None and all(isinstance(request[k], str) and request[k] for k in ('region', 'category')), 'sale ledger dimensions differ')
            else:
                require(isinstance(request['sale_id'], str) and request['sale_id'] and request['region'] is None and request['category'] is None, 'refund ledger payload differs')
                sale = sales.get(request['sale_id'], self.seed_sale(request['sale_id']))
                if sale is None: outcome = 'sale_not_found'
                elif request['event_time_s'] < sale['event_time_s']: outcome = 'refund_precedes_sale'
                elif refunded[request['sale_id']] + request['amount_minor'] > sale['amount_minor']: outcome = 'refund_exceeds_sale'
                else: event.update(region=sale['region'], category=sale['category']); refunded[request['sale_id']] += request['amount_minor']
            if outcome == 'recorded':
                events.append(event)
                if request['kind'] == 'sale': sales[event_id] = event
            require(operation['result'] == {'event_id': event_id, 'outcome': outcome, 'changed': outcome == 'recorded'}, 'SQL event outcome differs from independent replay')
        self.cache[key] = events; return events

    def verify_final(self):
        events = self.reduce(); require(self.state['changed_events'] == sorted(events, key=lambda row: row['event_id']), 'native mutable events differ')
        all_events = list(itertools.chain((self.sale(i) for i in range(self.rows)), events)); all_events.sort(key=lambda row: row['event_id'])
        require(self.state['observed_events'] == digest(all_events), 'native full event ledger differs')
        buckets = defaultdict(lambda: dict.fromkeys(self.metrics, 0))
        for event in all_events:
            value = buckets[event['event_time_s'], event['region'], event['category']]; sale = event['kind'] == 'sale'
            value['gross_total_minor' if sale else 'refund_total_minor'] += event['amount_minor']; value['sales_count' if sale else 'refund_count'] += 1
        expected = [{'event_time_s': key[0], 'region': key[1], 'category': key[2], **value} for key, value in sorted(buckets.items())]
        require(self.state['buckets'] == (expected if self.state['query_kind'] == 'maintained' else None), 'native exact event buckets differ')
        return {'status': 'passed', 'seed_events_checked': self.rows, 'seed_operations_checked': self.rows, 'mutable_operations': len(self.operations), 'integer_totals': True}

    def expected(self, snapshot, start_s, end_s, *, group_by=('region', 'category'), region=None, category=None, offset=0, limit=20):
        grouping = tuple(group_by)
        require(integer(start_s) and integer(end_s) and start_s < end_s and grouping in (('region',), ('category',), ('region', 'category')) and
            all(v is None or isinstance(v, str) for v in (region, category)) and integer(offset) and integer(limit, 1) and limit <= 100, 'analytics query parameters differ')
        key = start_s, end_s, grouping, region, category
        def selected(event): return start_s <= event['event_time_s'] < end_s and (region is None or event['region'] == region) and (category is None or event['category'] == category)
        def add(groups, event):
            value = groups.setdefault(tuple(event[name] for name in grouping), dict.fromkeys(self.metrics, 0)); sale = event['kind'] == 'sale'
            value['gross_total_minor' if sale else 'refund_total_minor'] += event['amount_minor']; value['sales_count' if sale else 'refund_count'] += 1
        if key not in self.windows:
            groups = {}; count = 0
            for i in range(self.rows):
                event = self.sale(i)
                if selected(event): add(groups, event); count += 1
            self.windows[key] = groups, count
        base, count = self.windows[key]; groups = {k: dict(v) for k, v in base.items()}
        for event in self.reduce(snapshot):
            if selected(event): add(groups, event); count += 1
        values = [{**dict(zip(grouping, k)), **v, 'net_total_minor': v['gross_total_minor'] - v['refund_total_minor']} for k, v in groups.items()]
        values.sort(key=lambda row: (-row['net_total_minor'], *(row[name] for name in grouping)))
        return {'groups': values[offset:offset + limit], 'groupsCount': len(values), 'eventCount': count, 'window': {'start_s': start_s, 'end_s': end_s, 'bounds': '[start,end)'}}

    def verify_query(self, evidence):
        require(evidence['read_operation'] == 'event_time_groups', 'analytics page identity differs')
        expected = self.expected(evidence['snapshot'], **evidence['parameters'])
        require(all(type(row[k]) is int for row in evidence['groups'] for k in (*self.metrics, 'net_total_minor')) and all(evidence[k] == value for k, value in expected.items()), 'analytics snapshot aggregates/order differ')
        return True
