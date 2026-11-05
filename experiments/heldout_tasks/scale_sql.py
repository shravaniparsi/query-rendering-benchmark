"""Deterministic scale seeds and compact native checks for the reserved tasks.

No performance collector is enabled here. Initial rows are reconstructable;
native digests cover every seeded/final row, while mutable ledgers remain full.
"""
import hashlib
import json

from .inventory import require, integer
from .inventory_sql import InventorySQL
from .analytics_sql import AnalyticsSQL, METRICS

SEED_VERSION = 'reserved-scale-seed-v1'


def rows_digest(rows):
    value = hashlib.sha256(); count = 0
    for row in rows:
        value.update(json.dumps(row, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode() + b'\n'); count += 1
    return {'rows': count, 'sha256': value.hexdigest()}


def size(rows): require(type(rows) is int and 100 <= rows <= 100000, 'bounded scale rows required')


class ScaleInventorySQL(InventorySQL):
    def apply_on(self, connection, operation_id, kind, reservation_id, *, product_id=None, quantity=None):
        require(isinstance(operation_id, str) and not operation_id.startswith('seed-'), 'seed operation namespace is reserved')
        if self.kind != 'maintained' or kind != 'reserve':
            return super().apply_on(connection, operation_id, kind, reservation_id, product_id=product_id, quantity=quantity)
        require(all(isinstance(value, str) and 0 < len(value) <= 128 for value in (operation_id, reservation_id)) and integer(product_id, 1) and integer(quantity, 1), 'invalid bounded reservation request')
        payload = dict(kind=kind, reservation_id=reservation_id, product_id=product_id, quantity=quantity)
        with connection.transaction():
            for token in ('inventory-operation:' + operation_id, 'inventory-reservation:' + reservation_id): connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', (token,))
            previous = connection.execute('SELECT request,result FROM operations WHERE operation_id=%s', (operation_id,)).fetchone()
            if previous:
                require(previous[0] == payload, 'idempotency identity reused with different payload'); return previous[1]
            existing = connection.execute('SELECT product_id FROM reservations WHERE reservation_id=%s', (reservation_id,)).fetchone()
            product = connection.execute('SELECT id FROM products WHERE id=%s FOR UPDATE', (product_id,)).fetchone(); require(product is not None, 'unknown product')
            if existing: outcome = 'reservation_exists'
            else:
                remaining = connection.execute('SELECT remaining_stock FROM stock_balances WHERE product_id=%s', (product_id,)).fetchone()[0]
                if remaining < quantity: outcome = 'insufficient_stock'
                else:
                    connection.execute("INSERT INTO reservations VALUES (%s,%s,%s,'active')", (reservation_id, product_id, quantity))
                    connection.execute('UPDATE stock_balances SET remaining_stock=remaining_stock-%s WHERE product_id=%s', (quantity, product_id)); outcome = 'reserved'
            result = dict(operation_id=operation_id, reservation_id=reservation_id, outcome=outcome, changed=outcome == 'reserved')
            connection.execute('INSERT INTO operations(operation_id,request,result,transaction_xid) VALUES (%s,%s::jsonb,%s::jsonb,pg_current_xact_id()::text::bigint)',
                (operation_id, json.dumps(payload), json.dumps(result)))
        return result

    def initialize_scale(self, rows, kind):
        size(rows); require(kind in ('correlated', 'batched', 'maintained'), 'unknown inventory query'); self.create_schema()
        seed = {'version': SEED_VERSION, 'task': 'inventory', 'rows': rows}
        with self.connection() as connection, connection.transaction():
            connection.execute('CREATE TABLE metadata (seed jsonb NOT NULL,query_kind text NOT NULL,seed_xid bigint NOT NULL)')
            xid = connection.execute('SELECT pg_current_xact_id()::text::bigint').fetchone()[0]
            connection.execute('INSERT INTO metadata VALUES (%s::jsonb,%s,%s)', (json.dumps(seed), kind, xid))
            connection.execute('''CREATE TABLE products (id integer PRIMARY KEY, category text NOT NULL, brand text NOT NULL,
                price_minor bigint NOT NULL CHECK(price_minor>=0), initial_stock bigint NOT NULL CHECK(initial_stock>=0))''')
            connection.execute('CREATE INDEX product_facets_price ON products(category,brand,price_minor,id)')
            connection.execute('''CREATE TABLE reservations (reservation_id text PRIMARY KEY,product_id integer NOT NULL REFERENCES products,
                quantity bigint NOT NULL CHECK(quantity>0),status text NOT NULL CHECK(status IN ('active','cancelled')))''')
            connection.execute("CREATE INDEX active_product_reservations ON reservations(product_id) INCLUDE(quantity) WHERE status='active'")
            connection.execute('''CREATE TABLE operations (operation_id text PRIMARY KEY,request jsonb NOT NULL,result jsonb NOT NULL,
                operation_sequence bigint GENERATED ALWAYS AS IDENTITY UNIQUE,transaction_xid bigint NOT NULL)''')
            with connection.cursor().copy('COPY products FROM STDIN') as copy:
                for i in range(1, rows + 1): copy.write_row((i, 'category-' + str(i % 4), 'brand-' + str(i % 3), 100 + i * 37, 1 + i % 5))
            with connection.cursor().copy('COPY operations(operation_id,request,result,transaction_xid) FROM STDIN') as copy:
                for i in range(1, rows + 1):
                    op, reservation = 'seed-reserve-' + str(i), 'seed-reservation-' + str(i)
                    payload = dict(kind='reserve', reservation_id=reservation, product_id=i, quantity=1)
                    result = dict(operation_id=op, reservation_id=reservation, outcome='reserved', changed=True)
                    copy.write_row((op, json.dumps(payload), json.dumps(result), xid))
            with connection.cursor().copy('COPY reservations FROM STDIN') as copy:
                for i in range(1, rows + 1): copy.write_row(('seed-reservation-' + str(i), i, 1, 'active'))
            if kind == 'maintained':
                connection.execute('CREATE TABLE stock_balances (product_id integer PRIMARY KEY REFERENCES products,remaining_stock bigint NOT NULL CHECK(remaining_stock>=0))')
                connection.execute('INSERT INTO stock_balances SELECT id,initial_stock-1 FROM products')
        self.kind = kind

    def configuration(self):
        with self.connection() as connection: values = connection.execute('SELECT seed,query_kind,seed_xid FROM metadata').fetchall()
        require(len(values) == 1 and values[0][0]['version'] == SEED_VERSION and values[0][0]['task'] == 'inventory', 'scale inventory seed differs')
        self.kind = values[0][1]; return {'seed': values[0][0], 'query_kind': self.kind, 'seed_xid': values[0][2], 'read_operation': 'inventory_page'}

    def validation_state(self):
        from psycopg.rows import dict_row
        config = self.configuration()
        with self.connection() as connection, connection.transaction():
            connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            def selected(sql):
                with connection.cursor(row_factory=dict_row) as cursor: cursor.execute(sql); return cursor.fetchall()
            products = rows_digest(selected('SELECT * FROM products ORDER BY id'))
            seeded = rows_digest(selected("SELECT * FROM operations WHERE operation_id LIKE 'seed-reserve-%' ORDER BY operation_sequence"))
            operations = selected("SELECT * FROM operations WHERE operation_id NOT LIKE 'seed-reserve-%' ORDER BY operation_sequence")
            reservations = rows_digest(selected('SELECT * FROM reservations ORDER BY reservation_id COLLATE "C"'))
            balances = rows_digest(selected('SELECT * FROM stock_balances ORDER BY product_id')) if self.kind == 'maintained' else None
        return {'format': 'reserved-inventory-scale-state-v1', **config, 'operations': operations,
            'observed_products': products, 'observed_seed_operations': seeded, 'observed_reservations': reservations,
            'observed_balances': balances, 'comparative_timing': False}


class ScaleAnalyticsSQL(AnalyticsSQL):
    def append_on(self, connection, event_id, *args, **kwargs):
        require(isinstance(event_id, str) and not event_id.startswith('seed-'), 'seed event namespace is reserved')
        return super().append_on(connection, event_id, *args, **kwargs)

    def initialize_scale(self, rows, kind):
        size(rows); super().initialize(kind); seed = {'version': SEED_VERSION, 'task': 'analytics', 'rows': rows}
        with self.connection() as connection, connection.transaction():
            connection.execute('ALTER TABLE metadata ADD COLUMN seed jsonb, ADD COLUMN seed_xid bigint')
            xid = connection.execute('SELECT pg_current_xact_id()::text::bigint').fetchone()[0]
            connection.execute('UPDATE metadata SET seed=%s::jsonb,seed_xid=%s', (json.dumps(seed), xid))
            with connection.cursor().copy('COPY operations(event_id,request,result,transaction_xid) FROM STDIN') as copy:
                for i in range(rows):
                    event = 'seed-sale-' + str(i); payload = dict(event_id=event, kind='sale', event_time_s=100 + i % 3600,
                        amount_minor=1000 + i % 101, sale_id=None, region='region-' + str(i % 3), category='category-' + str(i % 4))
                    result = dict(event_id=event, outcome='recorded', changed=True)
                    copy.write_row((event, json.dumps(payload), json.dumps(result), xid))
            with connection.cursor().copy('COPY events FROM STDIN') as copy:
                for i in range(rows): copy.write_row(('seed-sale-' + str(i), 'sale', 100 + i % 3600, 1000 + i % 101, None, 'region-' + str(i % 3), 'category-' + str(i % 4)))
            if kind == 'maintained': connection.execute('''INSERT INTO event_buckets SELECT event_time_s,region,category,sum(amount_minor),0,count(*),0 FROM events GROUP BY event_time_s,region,category''')

    def configuration(self):
        with self.connection() as connection: values = connection.execute('SELECT seed,query_kind,seed_xid FROM metadata').fetchall()
        require(len(values) == 1 and values[0][0]['version'] == SEED_VERSION and values[0][0]['task'] == 'analytics', 'scale analytics seed differs')
        self.kind = values[0][1]; return {'seed': values[0][0], 'query_kind': self.kind, 'seed_xid': values[0][2], 'read_operation': 'event_time_groups'}

    def validation_state(self):
        from psycopg.rows import dict_row
        config = self.configuration()
        with self.connection() as connection, connection.transaction():
            connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            def selected(sql):
                with connection.cursor(row_factory=dict_row) as cursor: cursor.execute(sql); return cursor.fetchall()
            seeded = rows_digest(selected("SELECT * FROM operations WHERE event_id LIKE 'seed-sale-%' ORDER BY operation_sequence"))
            operations = selected("SELECT * FROM operations WHERE event_id NOT LIKE 'seed-sale-%' ORDER BY operation_sequence")
            events = rows_digest(selected('SELECT * FROM events ORDER BY event_id COLLATE "C"'))
            changed = selected("SELECT * FROM events WHERE event_id NOT LIKE 'seed-sale-%' ORDER BY event_id COLLATE \"C\"")
            if self.kind == 'maintained':
                buckets = selected('SELECT * FROM event_buckets ORDER BY event_time_s,region COLLATE "C",category COLLATE "C"')
                require(all(row[key] == int(row[key]) for row in buckets for key in METRICS), 'noninteger native event bucket')
                buckets = [{**row, **{key: int(row[key]) for key in METRICS}} for row in buckets]
            else: buckets = None
        return {'format': 'reserved-analytics-scale-state-v1', **config, 'operations': operations, 'observed_seed_operations': seeded,
            'observed_events': events, 'changed_events': changed, 'buckets': buckets, 'comparative_timing': False}
