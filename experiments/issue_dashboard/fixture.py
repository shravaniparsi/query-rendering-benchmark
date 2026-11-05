"""Indexed issue/comment workload with three exact aggregation strategies."""
from experiments.query_rendering_load.fixture import Fixture

STATES = ('open','review','closed')
KINDS = ('correlated','batched','maintained')
COUNT_SQL = 'SELECT count(*) FROM issues WHERE project_id=%s AND state=%s'
PAGE = '''WITH page AS (
 SELECT id,project_id,assignee_id,title,state,revision FROM issues
 WHERE project_id=%s AND state=%s ORDER BY id DESC LIMIT %s OFFSET %s)
'''
FIELDS = '''p.id,p.project_id,p.title,p.state,p.revision,u.name AS assignee,
 pr.name AS project_name,'''
JOINS = ' FROM page p JOIN assignees u ON u.id=p.assignee_id JOIN projects pr ON pr.id=p.project_id '
QUERIES = {
    'correlated':PAGE+'SELECT '+FIELDS+''' stats.comment_count'''+JOINS+'''
 LEFT JOIN LATERAL (SELECT count(*) AS comment_count FROM comments c WHERE c.issue_id=p.id) stats ON true
 ORDER BY p.id DESC''',
    'batched':PAGE.rstrip()+''', totals AS (
 SELECT c.issue_id,count(*) AS comment_count FROM comments c JOIN page pp ON pp.id=c.issue_id GROUP BY c.issue_id)
 SELECT '''+FIELDS+' COALESCE(t.comment_count,0) AS comment_count'+JOINS+'''
 LEFT JOIN totals t ON t.issue_id=p.id ORDER BY p.id DESC''',
    'maintained':PAGE+'SELECT '+FIELDS+' s.comment_count'+JOINS+'''
 JOIN issue_counts s ON s.issue_id=p.id ORDER BY p.id DESC'''}


def validate_page(project_id,state,limit,offset):
    if type(project_id) is not int or not 0<=project_id<5 or state not in STATES:
        raise ValueError('invalid project/state')
    if type(limit) is not int or not 1<=limit<=100 or type(offset) is not int or not 0<=offset<=1000000:
        raise ValueError('invalid page bounds')


class IssueFixture(Fixture):
    def initialize(self,rows=1000,fanout=20,query_kind='correlated'):
        if type(rows) is not int or not 100<=rows<=100000 or type(fanout) is not int or not 0<=fanout<=200:
            raise ValueError('invalid fixture dimensions')
        if query_kind not in KINDS:
            raise ValueError('unknown aggregation strategy')
        with self.connection() as conn:
            conn.execute(self.driver.sql.SQL('CREATE SCHEMA {}').format(self.driver.sql.Identifier(self.schema)))
            conn.execute('CREATE TABLE fixture_metadata (issue_rows integer,fanout integer,query_kind text)')
            conn.execute('INSERT INTO fixture_metadata VALUES (%s,%s,%s)',(rows,fanout,query_kind))
            conn.execute('CREATE TABLE projects (id integer PRIMARY KEY,name text NOT NULL)')
            conn.execute('CREATE TABLE assignees (id integer PRIMARY KEY,name text NOT NULL)')
            conn.execute('''CREATE TABLE issues (id integer PRIMARY KEY,project_id integer NOT NULL REFERENCES projects,
                assignee_id integer NOT NULL REFERENCES assignees,title text NOT NULL,state text NOT NULL
                CHECK(state IN ('open','review','closed')),revision bigint NOT NULL DEFAULT 0)''')
            conn.execute('CREATE INDEX issues_project_state_id ON issues(project_id,state,id DESC)')
            conn.execute('CREATE INDEX issues_assignee ON issues(assignee_id)')
            conn.execute('''CREATE TABLE comments (id bigserial PRIMARY KEY,issue_id integer NOT NULL REFERENCES issues,
                body text NOT NULL)''')
            conn.execute('CREATE INDEX comments_issue ON comments(issue_id)')
            conn.execute('''CREATE TABLE operations (operation_id text PRIMARY KEY,issue_id integer NOT NULL,
                revision bigint NOT NULL,kind text NOT NULL,value text NOT NULL,comment_id bigint,
                transaction_xid xid8 NOT NULL DEFAULT pg_current_xact_id())''')
            with conn.cursor() as cursor:
                cursor.executemany('INSERT INTO projects VALUES (%s,%s)',[(i,f'Project {i}') for i in range(5)])
                cursor.executemany('INSERT INTO assignees VALUES (%s,%s)',[(i,f'Engineer {i:02d}') for i in range(17)])
                cursor.executemany('INSERT INTO issues VALUES (%s,%s,%s,%s,%s,0)',
                    [(i,i%5,i%17,f'Issue {i:07d} & discussion',STATES[(i//5)%3]) for i in range(rows)])
                # COPY bounds Python memory even at larger fanout.
                with cursor.copy('COPY comments (id,issue_id,body) FROM STDIN') as copy:
                    for issue_id in range(rows):
                        for ordinal in range(issue_id%(fanout+1)):
                            copy.write_row((issue_id*(fanout+1)+ordinal,issue_id,f'Seed discussion {ordinal}'))
            conn.execute("SELECT setval(pg_get_serial_sequence('comments','id'),%s,true)",(rows*(fanout+1),))
            if query_kind=='maintained':
                conn.execute('''CREATE TABLE issue_counts (issue_id integer PRIMARY KEY REFERENCES issues,
                    comment_count bigint NOT NULL CHECK(comment_count>=0))''')
                conn.execute('''INSERT INTO issue_counts SELECT i.id,count(c.id) FROM issues i
                    LEFT JOIN comments c ON c.issue_id=i.id GROUP BY i.id''')
            for table in ('issues','comments','projects','assignees'):
                conn.execute(self.driver.sql.SQL('ANALYZE {}').format(self.driver.sql.Identifier(table)))

    def configuration(self):
        with self.connection() as conn:
            row = conn.execute('SELECT issue_rows,fanout,query_kind FROM fixture_metadata').fetchone()
        return dict(zip(('rows','fanout','query_kind'),row))

    def page_on(self,conn,query_kind,project_id=0,state='open',limit=20,offset=0):
        validate_page(project_id,state,limit,offset)
        if query_kind not in QUERIES:
            raise ValueError('unknown query strategy')
        from psycopg.rows import dict_row
        with conn.transaction():
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            with conn.cursor(row_factory=dict_row) as cursor:
                cursor.execute('SELECT pg_current_snapshot()::text AS snapshot')
                snapshot = cursor.fetchone()['snapshot']
                cursor.execute(COUNT_SQL,(project_id,state))
                count = cursor.fetchone()['count']
                cursor.execute(QUERIES[query_kind],(project_id,state,limit,offset))
                issues = cursor.fetchall()
        return {'snapshot':snapshot,'issues':issues,'issuesCount':count,
                'project_id':project_id,'state':state,'limit':limit,'offset':offset}

    def page(self,project_id=0,state='open',limit=20,offset=0):
        config = self.configuration()
        with self.driver.connect(self.dsn,autocommit=True) as conn:
            conn.execute(self.driver.sql.SQL('SET search_path TO {}, public').format(self.driver.sql.Identifier(self.schema)))
            return self.page_on(conn,config['query_kind'],project_id,state,limit,offset)

    def write_on(self,conn,query_kind,operation_id,issue_id,kind,value,*,phase_call=None):
        if not isinstance(operation_id,str) or not 1<=len(operation_id)<=128 or type(issue_id) is not int:
            raise ValueError('invalid operation identity')
        if kind not in ('comment','state') or not isinstance(value,str) or not 1<=len(value)<=2000:
            raise ValueError('invalid mutation')
        if kind=='state' and value not in STATES:
            raise ValueError('invalid state')
        if query_kind not in KINDS:
            raise ValueError('unknown query strategy')
        def run(label,*args):
            return conn.execute(*args) if phase_call is None else phase_call(label,conn.execute,*args)
        run('operation_lock','SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(operation_id,))
        previous = run('retry_lookup','SELECT issue_id,revision,kind,value,comment_id FROM operations WHERE operation_id=%s',
                                (operation_id,)).fetchone()
        if previous:
            if (previous[0],previous[2],previous[3])!=(issue_id,kind,value):
                raise ValueError('conflicting operation retry')
            return {'operation_id':operation_id,'revision':previous[1],'comment_id':previous[4],'changed':True}
        before = run('issue_lock','SELECT state,revision FROM issues WHERE id=%s FOR UPDATE',(issue_id,)).fetchone()
        if not before or (kind=='state' and before[0]==value):
            raise ValueError('missing issue or unchanged assignment')
        comment_id = None
        if kind=='comment':
            comment_id = run('comment_insert','INSERT INTO comments (issue_id,body) VALUES (%s,%s) RETURNING id',
                                      (issue_id,value)).fetchone()[0]
            if query_kind=='maintained':
                updated = run('count_update','UPDATE issue_counts SET comment_count=comment_count+1 WHERE issue_id=%s',
                                       (issue_id,))
                if updated.rowcount!=1:
                    raise RuntimeError('missing summary row')
            revision = run('issue_revision_update','UPDATE issues SET revision=revision+1 WHERE id=%s RETURNING revision',
                                    (issue_id,)).fetchone()[0]
        else:
            revision = run('issue_state_update','UPDATE issues SET state=%s,revision=revision+1 WHERE id=%s RETURNING revision',
                                    (value,issue_id)).fetchone()[0]
        run('ledger_insert','''INSERT INTO operations (operation_id,issue_id,revision,kind,value,comment_id)
            VALUES (%s,%s,%s,%s,%s,%s)''',(operation_id,issue_id,revision,kind,value,comment_id))
        return {'operation_id':operation_id,'revision':revision,'comment_id':comment_id,'changed':True}

    def write(self,operation_id,issue_id,kind,value):
        config = self.configuration()
        with self.connection() as conn:
            return self.write_on(conn,config['query_kind'],operation_id,issue_id,kind,value)

    def prepare_maintenance(self):
        config = self.configuration()
        tables = ['issues','comments','projects','assignees','operations']
        if config['query_kind']=='maintained': tables.append('issue_counts')
        with self.driver.connect(self.dsn,autocommit=True) as conn:
            for table in tables:
                conn.execute(self.driver.sql.SQL('VACUUM (ANALYZE) {}').format(self.driver.sql.Identifier(self.schema,table)))

    def diagnostics(self,project_id=0,state='open'):
        config = self.configuration()
        with self.connection() as conn:
            checkpoint=conn.execute('''SELECT checkpoint_lsn::text,redo_lsn::text,checkpoint_time::text,
                full_page_writes FROM pg_control_checkpoint()''').fetchone()
            relations=conn.execute('''SELECT relname,relpages,reltuples,relallvisible FROM pg_class
                WHERE relnamespace=%s::regnamespace AND relkind='r' ORDER BY relname''',(self.schema,)).fetchall()
            return {'configuration':config,'version':conn.execute('SELECT version()').fetchone()[0],
                'checkpoint':dict(zip(('checkpoint_lsn','redo_lsn','checkpoint_time','full_page_writes'),checkpoint)),
                'relation_planner_metadata':relations,
                'count_plan':conn.execute('EXPLAIN (FORMAT JSON) '+COUNT_SQL,(project_id,state)).fetchone()[0],
                'page_plan':conn.execute('EXPLAIN (FORMAT JSON) '+QUERIES[config['query_kind']],
                                         (project_id,state,20,0)).fetchone()[0],
                'indexes':conn.execute('SELECT indexname,indexdef FROM pg_indexes WHERE schemaname=%s ORDER BY indexname',
                                      (self.schema,)).fetchall()}

    def checkpoint(self):
        """Disposable calibration precondition, never part of a timed operation."""
        with self.driver.connect(self.dsn,autocommit=True) as conn:conn.execute('CHECKPOINT')

    def validation_state(self):
        config = self.configuration()
        from psycopg.rows import dict_row
        with self.connection() as conn:
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            with conn.cursor(row_factory=dict_row) as cursor:
                cursor.execute('''SELECT operation_id,issue_id,revision,kind,value,comment_id,
                    transaction_xid::text AS transaction_xid FROM operations ORDER BY issue_id,revision''')
                ledger = cursor.fetchall()
                cursor.execute('SELECT id,state,revision FROM issues WHERE revision>0 ORDER BY id')
                changed = cursor.fetchall()
                cursor.execute('SELECT id,issue_id,body FROM comments WHERE id>%s ORDER BY id',
                               (config['rows']*(config['fanout']+1),))
                appended = cursor.fetchall()
                summary = None
                if config['query_kind']=='maintained':
                    cursor.execute('SELECT issue_id,comment_count FROM issue_counts ORDER BY issue_id')
                    summary = cursor.fetchall()
        return {'format':'issue-state-v1',**config,'ledger':ledger,'changed_issues':changed,
                'appended_comments':appended,'summary_counts':summary,
                'scope':'Committed top-level xid8; append-only comments and issue-state changes; no deletes/prepared transactions.'}

    def drop(self):
        with self.connection() as conn:
            conn.execute(self.driver.sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(self.driver.sql.Identifier(self.schema)))
