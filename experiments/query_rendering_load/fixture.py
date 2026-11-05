"""PostgreSQL fixture with identical SQL in both physical index treatments.

Requires psycopg 3 at execution time. Initialization refuses an existing schema.
The dedicated schema must not contain unrelated data.
"""
import argparse
import json
import os
import re
from contextlib import contextmanager

TAGS = ('rendering', 'accessibility', 'databases', 'systems')
COUNT_SQL = 'SELECT count(*) FROM articles WHERE tag = %s'
PAGE_SQL = '''SELECT a.id, a.title, a.tag, a.revision, u.name AS author
 FROM articles a JOIN authors u ON u.id = a.author_id
 WHERE a.tag = %s ORDER BY a.id DESC LIMIT %s OFFSET %s'''


def seed_rows(size):
    if not isinstance(size, int) or isinstance(size, bool) or not 40 <= size <= 1_000_000:
        raise ValueError('size must be an integer between 40 and 1000000')
    return [(i, f'Engineering note {i:07d}',
             ('Measured application behavior. ' * 32), TAGS[i % 4], i % 20, 0)
            for i in range(size)]


def validate_page(tag, limit, offset):
    if tag not in TAGS or type(limit) is not int or type(offset) is not int:
        raise ValueError('invalid page arguments')
    if not 1 <= limit <= 100 or not 0 <= offset <= 1_000_000:
        raise ValueError('invalid page bounds')


class Fixture:
    def __init__(self, dsn, schema):
        if not re.fullmatch(r'qrl_[a-z][a-z0-9_]{0,40}', schema):
            raise ValueError('schema must start qrl_ and use lowercase identifier characters')
        import psycopg
        self.driver = psycopg
        self.dsn, self.schema = dsn, schema

    @contextmanager
    def connection(self):
        # One connection per control operation. This is not the timed service pool.
        with self.driver.connect(self.dsn) as conn:
            conn.execute(self.driver.sql.SQL('SET LOCAL search_path TO {}, public').format(
                self.driver.sql.Identifier(self.schema)))
            yield conn

    def initialize(self, size=100_000, indexed=False):
        rows = seed_rows(size)
        with self.connection() as conn:
            conn.execute(self.driver.sql.SQL('CREATE SCHEMA {}').format(
                self.driver.sql.Identifier(self.schema)))
            conn.execute('CREATE TABLE authors (id integer PRIMARY KEY, name text NOT NULL)')
            conn.execute('''CREATE TABLE articles (
                id integer PRIMARY KEY, title text NOT NULL, body text NOT NULL,
                tag text NOT NULL, author_id integer NOT NULL REFERENCES authors(id),
                revision bigint NOT NULL DEFAULT 0)''')
            conn.execute('CREATE INDEX articles_author_id ON articles(author_id)')
            conn.execute('''CREATE TABLE operations (
                operation_id text PRIMARY KEY, article_id integer NOT NULL,
                revision bigint NOT NULL, field text NOT NULL, value text NOT NULL,
                transaction_xid xid8 NOT NULL DEFAULT pg_current_xact_id())''')
            with conn.cursor() as cursor:
                cursor.executemany('INSERT INTO authors VALUES (%s,%s)',
                                   [(i, f'Author {i:02d}') for i in range(20)])
                cursor.executemany('INSERT INTO articles VALUES (%s,%s,%s,%s,%s,%s)', rows)
            if indexed:
                conn.execute('CREATE INDEX articles_tag_id ON articles(tag,id DESC)')
            conn.execute('ANALYZE articles')

    def page(self, tag='rendering', limit=20, offset=0):
        validate_page(tag, limit, offset)
        from psycopg.rows import dict_row
        with self.connection() as conn:
            # SET TRANSACTION precedes any query; SET LOCAL search_path is allowed.
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            with conn.cursor(row_factory=dict_row) as cursor:
                cursor.execute('SELECT pg_current_snapshot()::text AS snapshot')
                snapshot = cursor.fetchone()['snapshot']
                cursor.execute(COUNT_SQL, (tag,))
                count = cursor.fetchone()['count']
                cursor.execute(PAGE_SQL, (tag, limit, offset))
                return {'articles': cursor.fetchall(), 'articlesCount': count,
                        'snapshot': snapshot}

    def update(self, operation_id, article_id, field, value):
        with self.connection() as conn:
            return self.update_on(conn, operation_id, article_id, field, value)

    def update_on(self, conn, operation_id, article_id, field, value):
        if not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 128:
            raise ValueError('operation ID required, maximum 128 characters')
        if type(article_id) is not int or field not in ('tag', 'title'):
            raise ValueError('invalid update')
        if not isinstance(value, str) or not 1 <= len(value) <= 200:
            raise ValueError('invalid update value')
        if field == 'tag' and value not in TAGS:
            raise ValueError('invalid tag')
        # Concurrent retries serialize on their ID. Hash collisions only serialize
        # unrelated operations; the full ID remains the unique ledger key.
        conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', (operation_id,))
        previous = conn.execute('''SELECT article_id, revision, field, value
            FROM operations WHERE operation_id=%s''', (operation_id,)).fetchone()
        if previous:
            if (previous[0], previous[2], previous[3]) != (article_id, field, value):
                raise ValueError('operation ID reused with different payload')
            return {'operation_id': operation_id, 'revision': previous[1], 'changed': True}
        before = conn.execute(self.driver.sql.SQL('SELECT {}, revision FROM articles WHERE id=%s FOR UPDATE').format(
            self.driver.sql.Identifier(field)), (article_id,)).fetchone()
        if before is None:
            raise ValueError('article does not exist')
        if before[0] == value:
            raise ValueError('update must change the selected value')
        row = conn.execute(self.driver.sql.SQL('''UPDATE articles SET {}=%s,
            revision=revision+1 WHERE id=%s RETURNING revision''').format(
                self.driver.sql.Identifier(field)), (value, article_id)).fetchone()
        if row is None:
            raise ValueError('article does not exist')
        conn.execute('INSERT INTO operations (operation_id,article_id,revision,field,value) VALUES (%s,%s,%s,%s,%s)',
                     (operation_id, article_id, row[0], field, value))
        return {'operation_id': operation_id, 'revision': row[0], 'changed': True}

    def reset_seed(self, size):
        """Control-only reset; call after all requests drain, outside measurement."""
        rows = seed_rows(size)
        with self.connection() as conn:
            if conn.execute('SELECT count(*) FROM articles').fetchone()[0] != size:
                raise ValueError('reset size does not match fixture')
            conn.execute('DELETE FROM operations')
            with conn.cursor() as cursor:
                cursor.executemany('''UPDATE articles SET title=%s, body=%s, tag=%s,
                    author_id=%s, revision=%s WHERE id=%s''',
                    [(title, body, tag, author, revision, article_id)
                     for article_id, title, body, tag, author, revision in rows])

    def diagnostics(self, tag='rendering'):
        validate_page(tag, 20, 0)
        with self.connection() as conn:
            plan = conn.execute('EXPLAIN (FORMAT JSON) ' + PAGE_SQL, (tag, 20, 0)).fetchone()[0]
            count_plan = conn.execute('EXPLAIN (FORMAT JSON) '+COUNT_SQL,(tag,)).fetchone()[0]
            indexes = conn.execute('''SELECT indexname, indexdef FROM pg_indexes
                WHERE schemaname=%s ORDER BY indexname''', (self.schema,)).fetchall()
            version = conn.execute('SELECT version()').fetchone()[0]
            relation = conn.execute('''SELECT relpages,reltuples,relallvisible FROM pg_class
                WHERE oid=%s::regclass''',(self.schema+'.articles',)).fetchone()
            checkpoint = conn.execute('''SELECT checkpoint_lsn::text,redo_lsn::text,
                checkpoint_time::text,full_page_writes FROM pg_control_checkpoint()''').fetchone()
            return {'version': version, 'plan': plan, 'indexes': indexes,
                    'plans':{'count':count_plan,'page':plan},
                    'relation_planner_metadata':dict(zip(('relpages','reltuples','relallvisible'),relation)),
                    'checkpoint':dict(zip(('checkpoint_lsn','redo_lsn','checkpoint_time','full_page_writes'),checkpoint)),
                    'timed_service_ready': False}

    def prepare_maintenance(self, mode):
        if mode not in ('analyze_only','vacuum_analyze'):
            raise ValueError('unknown maintenance mode')
        if mode=='vacuum_analyze':
            with self.driver.connect(self.dsn,autocommit=True,application_name='qrl_maintenance') as conn:
                for table in ('articles','authors','operations'):
                    conn.execute(self.driver.sql.SQL('VACUUM (ANALYZE) {}').format(
                        self.driver.sql.Identifier(self.schema,table)))

    def validation_state(self, size):
        from psycopg.rows import dict_row
        with self.connection() as conn:
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            with conn.cursor(row_factory=dict_row) as cursor:
                cursor.execute('''SELECT operation_id,article_id,revision,field,value,
                    transaction_xid::text AS transaction_xid FROM operations ORDER BY operation_id''')
                ledger=cursor.fetchall()
                cursor.execute('SELECT id,title,tag,revision FROM articles WHERE revision>0 ORDER BY id')
                changed=cursor.fetchall()
        return {'format':'qrl-validation-state-v1','seed_rows':size,'ledger':ledger,'changed_rows':changed,
                'scope':'Committed ledger only; top-level xid8; unique-entity writes; no prepared transactions or logical deletes.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('initialize', 'check'))
    parser.add_argument('--schema', required=True)
    parser.add_argument('--size', type=int, default=100_000)
    parser.add_argument('--indexed', action='store_true')
    args = parser.parse_args()
    dsn = os.environ.get('QRL_DATABASE_URL')
    if not dsn:
        parser.error('set QRL_DATABASE_URL; credentials are never printed')
    fixture = Fixture(dsn, args.schema)
    if args.command == 'initialize':
        fixture.initialize(args.size, args.indexed)
    print(json.dumps({'page': fixture.page(), 'diagnostics': fixture.diagnostics()}))


if __name__ == '__main__':
    main()
