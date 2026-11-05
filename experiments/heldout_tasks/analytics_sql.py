"""Reserved event-time SQL semantics; correctness work without latency collection."""
import json
from .analytics import AnalyticsOracle
from .inventory import integer, require
from .postgres import ReservedSQL
from experiments.query_rendering_load.snapshot_oracle import visible

KINDS = ('correlated', 'batched', 'maintained')
OPERATION = 'event_time_groups'
METRICS = ('gross_total_minor', 'refund_total_minor', 'sales_count', 'refund_count')


def query_arguments(start_s, end_s, *, group_by=('region', 'category'), region=None, category=None, offset=0, limit=20):
    require(integer(start_s) and integer(end_s) and start_s < end_s, 'nonempty half-open integer time window required')
    require(isinstance(group_by, (tuple, list)) and tuple(group_by) in (('region',), ('category',), ('region', 'category')), 'unsupported grouping')
    require(all(v is None or isinstance(v, str) for v in (region, category)) and integer(offset) and integer(limit, 1) and limit <= 100, 'invalid analytics filters/page')
    return dict(start_s=start_s, end_s=end_s, group_by=list(group_by), region=region, category=category, offset=offset, limit=limit)


class AnalyticsSQL(ReservedSQL):
    def initialize(self, kind):
        require(kind in KINDS, 'unknown analytics query kind'); self.create_schema()
        with self.connection() as connection, connection.transaction():
            connection.execute('CREATE TABLE metadata (query_kind text NOT NULL)'); connection.execute('INSERT INTO metadata VALUES (%s)', (kind,))
            connection.execute('''CREATE TABLE operations (event_id text PRIMARY KEY, request jsonb NOT NULL, result jsonb NOT NULL,
                operation_sequence bigint GENERATED ALWAYS AS IDENTITY UNIQUE, transaction_xid bigint NOT NULL)''')
            connection.execute('''CREATE TABLE events (event_id text PRIMARY KEY REFERENCES operations, kind text NOT NULL CHECK(kind IN ('sale','refund')),
                event_time_s bigint NOT NULL CHECK(event_time_s>=0), amount_minor bigint NOT NULL CHECK(amount_minor>0),
                sale_id text REFERENCES events, region text NOT NULL, category text NOT NULL,
                CHECK((kind='sale' AND sale_id IS NULL) OR (kind='refund' AND sale_id IS NOT NULL)))''')
            connection.execute('CREATE INDEX event_window_dims ON events(event_time_s,region,category) INCLUDE(kind,amount_minor)')
            connection.execute("CREATE INDEX refunds_for_sale ON events(sale_id) INCLUDE(amount_minor) WHERE kind='refund'")
            if kind == 'maintained':
                connection.execute('''CREATE TABLE event_buckets (event_time_s bigint NOT NULL, region text NOT NULL, category text NOT NULL,
                    gross_total_minor numeric NOT NULL CHECK(gross_total_minor>=0), refund_total_minor numeric NOT NULL CHECK(refund_total_minor>=0),
                    sales_count bigint NOT NULL CHECK(sales_count>=0), refund_count bigint NOT NULL CHECK(refund_count>=0),
                    PRIMARY KEY(event_time_s,region,category),
                    CHECK(gross_total_minor=trunc(gross_total_minor) AND refund_total_minor=trunc(refund_total_minor)))''')
        self.kind = kind

    def configuration(self):
        with self.connection() as connection: values = connection.execute('SELECT query_kind FROM metadata').fetchall()
        require(len(values) == 1 and values[0][0] in KINDS, 'analytics metadata differs'); self.kind = values[0][0]
        return {'query_kind': self.kind, 'read_operation': OPERATION, 'bucket_width_s': 1 if self.kind == 'maintained' else None}

    def append_on(self, connection, event_id, kind, event_time_s, amount_minor, *, sale_id=None, region=None, category=None):
        require(isinstance(event_id, str) and 0 < len(event_id) <= 128 and kind in ('sale', 'refund'), 'bounded event identity/kind required')
        require(integer(event_time_s) and integer(amount_minor, 1), 'integer event time and positive amount required')
        if kind == 'sale': require(sale_id is None and all(isinstance(v, str) and v for v in (region, category)), 'sale dimensions required')
        else: require(isinstance(sale_id, str) and sale_id and region is None and category is None, 'refund must inherit sale dimensions')
        request = dict(event_id=event_id, kind=kind, event_time_s=event_time_s, amount_minor=amount_minor, sale_id=sale_id, region=region, category=category)
        with connection.transaction():
            connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('analytics-event:' + event_id,))
            previous = connection.execute('SELECT request,result FROM operations WHERE event_id=%s', (event_id,)).fetchone()
            if previous:
                require(previous[0] == request, 'event identity reused with different payload'); return previous[1]
            # The same sale-identity lock covers initial sale creation and refunds,
            # including a refund that observes an as-yet absent sale.
            connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('analytics-sale:' + (event_id if kind == 'sale' else sale_id),))
            outcome = 'recorded'
            if kind == 'refund':
                sale = connection.execute("SELECT event_time_s,amount_minor,region,category FROM events WHERE event_id=%s AND kind='sale' FOR UPDATE", (sale_id,)).fetchone()
                refunded = connection.execute("SELECT COALESCE(sum(amount_minor),0) FROM events WHERE sale_id=%s AND kind='refund'", (sale_id,)).fetchone()[0]
                if sale is None: outcome = 'sale_not_found'
                elif event_time_s < sale[0]: outcome = 'refund_precedes_sale'
                elif refunded + amount_minor > sale[1]: outcome = 'refund_exceeds_sale'
                else: region, category = sale[2], sale[3]
            result = dict(event_id=event_id, outcome=outcome, changed=outcome == 'recorded')
            connection.execute('INSERT INTO operations(event_id,request,result,transaction_xid) VALUES (%s,%s::jsonb,%s::jsonb,pg_current_xact_id()::text::bigint)',
                (event_id, json.dumps(request), json.dumps(result)))
            if outcome == 'recorded':
                connection.execute('INSERT INTO events VALUES (%s,%s,%s,%s,%s,%s,%s)', (event_id, kind, event_time_s, amount_minor, sale_id, region, category))
                if self.kind == 'maintained':
                    increments = (amount_minor, 0, 1, 0) if kind == 'sale' else (0, amount_minor, 0, 1)
                    connection.execute('''INSERT INTO event_buckets VALUES (%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT(event_time_s,region,category) DO UPDATE SET
                        gross_total_minor=event_buckets.gross_total_minor+EXCLUDED.gross_total_minor,
                        refund_total_minor=event_buckets.refund_total_minor+EXCLUDED.refund_total_minor,
                        sales_count=event_buckets.sales_count+EXCLUDED.sales_count,
                        refund_count=event_buckets.refund_count+EXCLUDED.refund_count''', (event_time_s, region, category, *increments))
        return result

    def append(self, *args, **kwargs):
        with self.connection() as connection: return self.append_on(connection, *args, **kwargs)

    def query_on(self, connection, start_s, end_s, **kwargs):
        args = query_arguments(start_s, end_s, **kwargs); columns = args['group_by']; groups = ','.join(columns)
        terms = ['event_time_s>=%s', 'event_time_s<%s']; values = [start_s, end_s]
        for key in ('region', 'category'):
            if args[key] is not None: terms.append(key + '=%s'); values.append(args[key])
        source = 'event_buckets' if self.kind == 'maintained' else 'events'
        selected = 'SELECT * FROM ' + source + ' WHERE ' + ' AND '.join(terms)
        if self.kind == 'correlated':
            equality = ' AND '.join('e.' + key + '=g.' + key for key in columns)
            expressions = [f"(SELECT COALESCE(sum(amount_minor),0) FROM selected e WHERE {equality} AND kind='{kind}') {field}" for kind, field in (('sale', 'gross_total_minor'), ('refund', 'refund_total_minor'))]
            expressions += [f"(SELECT count(*) FROM selected e WHERE {equality} AND kind='{kind}') {field}" for kind, field in (('sale', 'sales_count'), ('refund', 'refund_count'))]
            metrics = 'SELECT ' + ','.join('g.' + key for key in columns) + ',' + ','.join(expressions) + ' FROM (SELECT DISTINCT ' + groups + ' FROM selected) g'
        else:
            expressions = [f'sum({field}) {field}' for field in METRICS] if self.kind == 'maintained' else [
                "sum(CASE WHEN kind='sale' THEN amount_minor ELSE 0 END) gross_total_minor", "sum(CASE WHEN kind='refund' THEN amount_minor ELSE 0 END) refund_total_minor",
                "count(*) FILTER(WHERE kind='sale') sales_count", "count(*) FILTER(WHERE kind='refund') refund_count"]
            metrics = 'SELECT ' + groups + ',' + ','.join(expressions) + ' FROM selected GROUP BY ' + groups
        ordering = 'net_total_minor DESC,' + ','.join(key + ' COLLATE "C"' for key in columns)
        event_count = 'COALESCE(sum(sales_count+refund_count),0)' if self.kind == 'maintained' else 'count(*)'
        sql = 'WITH selected AS MATERIALIZED (' + selected + '), metrics AS (' + metrics + '''), ranked AS (
            SELECT *,gross_total_minor-refund_total_minor net_total_minor FROM metrics), page AS (
            SELECT * FROM ranked ORDER BY ''' + ordering + ''' LIMIT %s OFFSET %s)
            SELECT jsonb_build_object('groups',COALESCE((SELECT jsonb_agg(p ORDER BY ''' + ordering + ''') FROM page p),'[]'::jsonb),
                'groupsCount',(SELECT count(*) FROM ranked),'eventCount',(SELECT ''' + event_count + ''' FROM selected))'''
        with connection.transaction():
            connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            snapshot = connection.execute('SELECT pg_current_snapshot()::text').fetchone()[0]
            result = connection.execute(sql, (*values, args['limit'], args['offset'])).fetchone()[0]
        return {'snapshot': snapshot, 'read_operation': OPERATION, 'parameters': args, **result,
            'window': {'start_s': start_s, 'end_s': end_s, 'bounds': '[start,end)'}}

    def query(self, *args, **kwargs):
        with self.connection() as connection: return self.query_on(connection, *args, **kwargs)

    def validation_state(self):
        from psycopg.rows import dict_row
        with self.connection() as connection, connection.transaction():
            connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute('SELECT * FROM operations ORDER BY operation_sequence'); operations = cursor.fetchall()
                cursor.execute('SELECT e.*,o.operation_sequence FROM events e JOIN operations o USING(event_id) ORDER BY o.operation_sequence'); events = cursor.fetchall()
                if self.kind == 'maintained':
                    cursor.execute('SELECT * FROM event_buckets ORDER BY event_time_s,region COLLATE "C",category COLLATE "C"'); buckets = cursor.fetchall()
                    require(all(row[key] == int(row[key]) for row in buckets for key in METRICS), 'noninteger native event bucket')
                    buckets = [{**row, **{key: int(row[key]) for key in METRICS}} for row in buckets]
                else: buckets = None
            return {'format': 'reserved-analytics-sql-v1', 'query_kind': self.kind, 'operations': operations,
                'raw_events': events, 'buckets': buckets, 'comparative_timing': False,
                'ordering_scope': 'Independent replay follows committed operation-sequence order; event-time windows never use this order.'}


def reconstruct(state, snapshot=None):
    require(state['format'] == 'reserved-analytics-sql-v1' and state['query_kind'] in KINDS, 'analytics state identity differs')
    oracle = AnalyticsOracle(); seen = set(); last = 0
    for row in state['operations']:
        require(row['event_id'] not in seen and type(row['operation_sequence']) is int and row['operation_sequence'] > last and int(row['transaction_xid']) > 0, 'event identity/order differs')
        seen.add(row['event_id']); last = row['operation_sequence']
        require(row['request']['event_id'] == row['event_id'], 'event payload identity differs')
        if snapshot is None or visible(row['transaction_xid'], snapshot):
            result = oracle.append(**row['request']); require(result == row['result'], 'SQL event outcome differs from independent replay')
    return oracle


def verify_final(state):
    from collections import defaultdict
    oracle = reconstruct(state); expected = oracle.state()['events']; operations = {r['event_id']: r for r in state['operations']}
    actual = []
    for sequence, row in enumerate(state['raw_events']):
        require(row['operation_sequence'] == operations[row['event_id']]['operation_sequence'], 'event/operation sequence differs')
        actual.append({k: v for k, v in row.items() if k != 'operation_sequence'} | {'ingest_sequence': sequence})
    require(actual == expected, 'SQL event rows differ from independent replay')
    buckets = defaultdict(lambda: dict.fromkeys(METRICS, 0))
    for event in expected:
        value = buckets[event['event_time_s'], event['region'], event['category']]; sale = event['kind'] == 'sale'
        value['gross_total_minor' if sale else 'refund_total_minor'] += event['amount_minor']; value['sales_count' if sale else 'refund_count'] += 1
    expected_buckets = [{'event_time_s': key[0], 'region': key[1], 'category': key[2], **value} for key, value in sorted(buckets.items())]
    require(state['buckets'] == (expected_buckets if state['query_kind'] == 'maintained' else None), 'maintained event buckets differ')
    return {'status': 'passed', 'operations': len(state['operations']), 'events': len(actual), 'integer_totals': True}


def verify_query(state, evidence):
    require(evidence['read_operation'] == OPERATION, 'analytics read identity differs')
    parameters = {**evidence['parameters'], 'group_by': tuple(evidence['parameters']['group_by'])}
    expected = reconstruct(state, evidence['snapshot']).query(**parameters)
    require(all(type(row[k]) is int for row in evidence['groups'] for k in (*METRICS, 'net_total_minor')), 'analytics integer totals differ')
    require(all(evidence[k] == v for k, v in expected.items()), 'analytics snapshot totals/order/window differs'); return True
