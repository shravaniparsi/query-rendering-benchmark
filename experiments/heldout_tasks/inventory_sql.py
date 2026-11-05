"""Reserved inventory SQL implementation. Correctness only before policy release."""
import json

from .inventory import integer, require, InventoryOracle
from .postgres import ReservedSQL
from experiments.query_rendering_load.snapshot_oracle import visible

KINDS = ('correlated', 'batched', 'maintained')
OPERATION = 'inventory_page'


def query_arguments(category=None, brand=None, min_price_minor=0, max_price_minor=None, available_only=True, offset=0, limit=20):
    require(all(v is None or isinstance(v, str) for v in (category, brand)), 'invalid facets')
    require(integer(min_price_minor) and (max_price_minor is None or integer(max_price_minor) and max_price_minor >= min_price_minor), 'invalid integer price interval')
    require(type(available_only) is bool and integer(offset) and integer(limit, 1) and limit <= 100, 'invalid availability/page')
    return dict(category=category, brand=brand, min_price_minor=min_price_minor, max_price_minor=max_price_minor,
        available_only=available_only, offset=offset, limit=limit)


class InventorySQL(ReservedSQL):
    def initialize(self, products, kind):
        require(kind in KINDS, 'unknown inventory query kind')
        # Validate the seed independently of the application database schema.
        InventoryOracle(products)
        self.create_schema()
        with self.connection() as connection, connection.transaction():
            connection.execute('CREATE TABLE metadata (products jsonb NOT NULL, query_kind text NOT NULL)')
            connection.execute('INSERT INTO metadata VALUES (%s::jsonb,%s)', (json.dumps(products), kind))
            connection.execute('''CREATE TABLE products (id integer PRIMARY KEY, category text NOT NULL, brand text NOT NULL,
                price_minor bigint NOT NULL CHECK(price_minor>=0), initial_stock bigint NOT NULL CHECK(initial_stock>=0))''')
            connection.execute('CREATE INDEX product_facets_price ON products(category,brand,price_minor,id)')
            connection.execute('''CREATE TABLE reservations (reservation_id text PRIMARY KEY, product_id integer NOT NULL REFERENCES products,
                quantity bigint NOT NULL CHECK(quantity>0), status text NOT NULL CHECK(status IN ('active','cancelled')))''')
            connection.execute("CREATE INDEX active_product_reservations ON reservations(product_id) INCLUDE(quantity) WHERE status='active'")
            connection.execute('''CREATE TABLE operations (operation_id text PRIMARY KEY, request jsonb NOT NULL, result jsonb NOT NULL,
                operation_sequence bigint GENERATED ALWAYS AS IDENTITY UNIQUE, transaction_xid bigint NOT NULL)''')
            for product in products:
                connection.execute('INSERT INTO products VALUES (%s,%s,%s,%s,%s)', tuple(product[k] for k in ('id', 'category', 'brand', 'price_minor', 'initial_stock')))
            if kind == 'maintained':
                connection.execute('CREATE TABLE stock_balances (product_id integer PRIMARY KEY REFERENCES products, remaining_stock bigint NOT NULL CHECK(remaining_stock>=0))')
                connection.execute('INSERT INTO stock_balances SELECT id,initial_stock FROM products')
        self.kind = kind

    def configuration(self):
        with self.connection() as connection: rows = connection.execute('SELECT products,query_kind FROM metadata').fetchall()
        require(len(rows) == 1 and rows[0][1] in KINDS, 'inventory metadata differs')
        self.kind = rows[0][1]; return {'products': rows[0][0], 'query_kind': self.kind, 'read_operation': OPERATION}

    def apply_on(self, connection, operation_id, kind, reservation_id, *, product_id=None, quantity=None):
        require(all(isinstance(v, str) and 0 < len(v) <= 128 for v in (operation_id, reservation_id)), 'bounded operation/reservation identities required')
        require(kind in ('reserve', 'cancel'), 'unknown reservation operation')
        if kind == 'reserve': require(integer(product_id, 1) and integer(quantity, 1), 'invalid reservation request')
        else: require(product_id is None and quantity is None, 'cancel payload differs')
        request = dict(kind=kind, reservation_id=reservation_id, product_id=product_id, quantity=quantity)
        with connection.transaction():
            # Different operations/reservations share only their actual product lock.
            # Hash collisions can serialize independent work but cannot alter results.
            for token in ('inventory-operation:' + operation_id, 'inventory-reservation:' + reservation_id):
                connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', (token,))
            previous = connection.execute('SELECT request,result FROM operations WHERE operation_id=%s', (operation_id,)).fetchone()
            if previous:
                require(previous[0] == request, 'idempotency identity reused with different payload'); return previous[1]
            reservation = connection.execute('SELECT product_id,quantity,status FROM reservations WHERE reservation_id=%s', (reservation_id,)).fetchone()
            if kind == 'reserve':
                product = connection.execute('SELECT initial_stock FROM products WHERE id=%s FOR UPDATE', (product_id,)).fetchone()
                require(product is not None, 'unknown product')
                if reservation: outcome = 'reservation_exists'
                else:
                    used = connection.execute("SELECT COALESCE(sum(quantity),0) FROM reservations WHERE product_id=%s AND status='active'", (product_id,)).fetchone()[0]
                    if product[0] - used < quantity: outcome = 'insufficient_stock'
                    else:
                        connection.execute("INSERT INTO reservations VALUES (%s,%s,%s,'active')", (reservation_id, product_id, quantity))
                        if self.kind == 'maintained': connection.execute('UPDATE stock_balances SET remaining_stock=remaining_stock-%s WHERE product_id=%s', (quantity, product_id))
                        outcome = 'reserved'
            elif reservation is None: outcome = 'not_found'
            elif reservation[2] == 'cancelled': outcome = 'already_cancelled'
            else:
                connection.execute('SELECT id FROM products WHERE id=%s FOR UPDATE', (reservation[0],))
                connection.execute("UPDATE reservations SET status='cancelled' WHERE reservation_id=%s", (reservation_id,))
                if self.kind == 'maintained': connection.execute('UPDATE stock_balances SET remaining_stock=remaining_stock+%s WHERE product_id=%s', (reservation[1], reservation[0]))
                outcome = 'cancelled'
            result = dict(operation_id=operation_id, reservation_id=reservation_id, outcome=outcome, changed=outcome in ('reserved', 'cancelled'))
            connection.execute('INSERT INTO operations(operation_id,request,result,transaction_xid) VALUES (%s,%s::jsonb,%s::jsonb,pg_current_xact_id()::text::bigint)',
                (operation_id, json.dumps(request), json.dumps(result)))
        return result

    def apply(self, *args, **kwargs):
        with self.connection() as connection: return self.apply_on(connection, *args, **kwargs)

    def query_on(self, connection, **kwargs):
        args = query_arguments(**kwargs); terms = ['p.price_minor>=%s']; values = [args['min_price_minor']]
        for key in ('category', 'brand'):
            if args[key] is not None: terms.append('p.' + key + '=%s'); values.append(args[key])
        if args['max_price_minor'] is not None: terms.append('p.price_minor<=%s'); values.append(args['max_price_minor'])
        where = ' AND '.join(terms)
        if self.kind == 'correlated':
            raw = "SELECT p.*,p.initial_stock-(SELECT COALESCE(sum(r.quantity),0) FROM reservations r WHERE r.product_id=p.id AND r.status='active') AS remaining_stock FROM products p WHERE " + where
        elif self.kind == 'batched':
            raw = "WITH base AS MATERIALIZED (SELECT p.* FROM products p WHERE " + where + "), totals AS (SELECT r.product_id,sum(r.quantity) used FROM reservations r JOIN base p ON p.id=r.product_id WHERE r.status='active' GROUP BY r.product_id) SELECT p.*,p.initial_stock-COALESCE(t.used,0) remaining_stock FROM base p LEFT JOIN totals t ON t.product_id=p.id"
        elif self.kind == 'maintained': raw = 'SELECT p.*,s.remaining_stock FROM products p JOIN stock_balances s ON s.product_id=p.id WHERE ' + where
        else: raise ValueError('unknown inventory query kind')
        sql = '''WITH raw AS MATERIALIZED (''' + raw + '''), filtered AS MATERIALIZED (
            SELECT * FROM raw WHERE (%s=false OR remaining_stock>0)), page AS (
            SELECT * FROM filtered ORDER BY id LIMIT %s OFFSET %s)
            SELECT jsonb_build_object('items',COALESCE((SELECT jsonb_agg(p ORDER BY id) FROM page p),'[]'::jsonb),
                'itemsCount',(SELECT count(*) FROM filtered),'facets',jsonb_build_object(
                'category',COALESCE((SELECT jsonb_object_agg(category,n) FROM (SELECT category,count(*) n FROM filtered GROUP BY category) c),'{}'::jsonb),
                'brand',COALESCE((SELECT jsonb_object_agg(brand,n) FROM (SELECT brand,count(*) n FROM filtered GROUP BY brand) b),'{}'::jsonb)))'''
        with connection.transaction():
            connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            snapshot = connection.execute('SELECT pg_current_snapshot()::text').fetchone()[0]
            result = connection.execute(sql, (*values, args['available_only'], args['limit'], args['offset'])).fetchone()[0]
        return {'snapshot': snapshot, 'read_operation': OPERATION, 'parameters': args, **result}

    def query(self, **kwargs):
        with self.connection() as connection: return self.query_on(connection, **kwargs)

    def validation_state(self):
        from psycopg.rows import dict_row
        with self.connection() as connection, connection.transaction():
            connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            config = connection.execute('SELECT products,query_kind FROM metadata').fetchone()
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute('SELECT * FROM products ORDER BY id'); products = cursor.fetchall()
                cursor.execute('SELECT * FROM reservations ORDER BY reservation_id'); reservations = cursor.fetchall()
                cursor.execute('SELECT * FROM operations ORDER BY operation_sequence'); operations = cursor.fetchall()
                if self.kind == 'maintained': cursor.execute('SELECT * FROM stock_balances ORDER BY product_id'); balances = cursor.fetchall()
                else: balances = None
            return {'format': 'reserved-inventory-sql-v1', 'products': config[0], 'query_kind': config[1],
                'observed_products': products, 'reservations': reservations, 'operations': operations, 'balances': balances,
                'comparative_timing': False}

def reconstruct(state, snapshot=None):
    """Re-execute visible request payloads with the independent ledger oracle."""
    require(state['format'] == 'reserved-inventory-sql-v1' and state['query_kind'] in KINDS, 'inventory state identity differs')
    oracle = InventoryOracle(state['products']); seen = set(); last = 0
    for operation in state['operations']:
        require(operation['operation_id'] not in seen and type(operation['operation_sequence']) is int and operation['operation_sequence'] > last and int(operation['transaction_xid']) > 0, 'inventory operation identity/order differs')
        seen.add(operation['operation_id']); last = operation['operation_sequence']
        if snapshot is None or visible(operation['transaction_xid'], snapshot):
            result = oracle.apply(operation['operation_id'], **operation['request']); require(result == operation['result'], 'inventory SQL outcome differs from independent replay')
    return oracle


def verify_final(state):
    oracle = reconstruct(state); expected = oracle.state()
    require(state['observed_products'] == sorted(state['products'], key=lambda p: p['id']) and state['reservations'] == expected['reservations'], 'inventory rows differ')
    balances = [{'product_id': p['id'], 'remaining_stock': p['remaining_stock']} for p in expected['products']]
    require(state['balances'] == (balances if state['query_kind'] == 'maintained' else None), 'inventory maintained stock differs')
    return {'status': 'passed', 'operations': len(state['operations']), 'reservations': len(state['reservations']), 'stock_nonnegative': True}


def verify_query(state, evidence):
    require(evidence['read_operation'] == OPERATION, 'inventory read identity differs')
    expected = reconstruct(state, evidence['snapshot']).query(**evidence['parameters'])
    require(all(type(row[k]) is int for row in evidence['items'] for k in ('id', 'price_minor', 'initial_stock', 'remaining_stock')), 'inventory integer fields differ')
    require(all(evidence[k] == v for k, v in expected.items()), 'inventory snapshot rows/count/facets differ')
    return True
