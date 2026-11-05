"""Aggregate-before-page discussion ranking, separate from the legacy ID-page workload."""
import hashlib
from .fixture import IssueFixture,COUNT_SQL,FIELDS,JOINS,KINDS,validate_page
from .oracle import IssueOracle
from .service import IssueService
from experiments.query_rendering_load.snapshot_oracle import parse_snapshot,visible

OPERATION='discussion_rank'
FILTERED='WITH filtered AS (SELECT id,project_id,assignee_id,title,state,revision FROM issues WHERE project_id=%s AND state=%s) '
RANK_SQL={
    'correlated':FILTERED+'SELECT '+FIELDS+''' stats.comment_count
 FROM filtered p JOIN assignees u ON u.id=p.assignee_id JOIN projects pr ON pr.id=p.project_id
 CROSS JOIN LATERAL (SELECT count(*) AS comment_count FROM comments c WHERE c.issue_id=p.id) stats
 ORDER BY stats.comment_count DESC,p.id DESC LIMIT %s OFFSET %s''',
    'batched':FILTERED.rstrip()+''', totals AS (
 SELECT c.issue_id,count(*) AS comment_count FROM comments c JOIN filtered f ON f.id=c.issue_id GROUP BY c.issue_id)
 SELECT '''+FIELDS+''' COALESCE(t.comment_count,0) AS comment_count
 FROM filtered p JOIN assignees u ON u.id=p.assignee_id JOIN projects pr ON pr.id=p.project_id
 LEFT JOIN totals t ON t.issue_id=p.id
 ORDER BY comment_count DESC,p.id DESC LIMIT %s OFFSET %s''',
    'maintained':'''SELECT '''+FIELDS+''' s.comment_count
 FROM issue_counts s JOIN issues p ON p.id=s.issue_id
 JOIN assignees u ON u.id=p.assignee_id JOIN projects pr ON pr.id=p.project_id
 WHERE p.project_id=%s AND p.state=%s
 ORDER BY s.comment_count DESC,p.id DESC LIMIT %s OFFSET %s'''}


class RankingFixture(IssueFixture):
    def initialize(self,rows=1000,fanout=20,query_kind='correlated'):
        super().initialize(rows,fanout,query_kind)
        if query_kind=='maintained':
            with self.connection() as conn:
                conn.execute('CREATE INDEX issue_counts_rank ON issue_counts(comment_count DESC,issue_id DESC)')
                conn.execute('ANALYZE issue_counts')

    def ranked_on(self,conn,query_kind,project_id=0,state='open',limit=20,offset=0):
        validate_page(project_id,state,limit,offset)
        if query_kind not in KINDS:raise ValueError('unknown ranking query strategy')
        from psycopg.rows import dict_row
        with conn.transaction():
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            with conn.cursor(row_factory=dict_row) as cursor:
                cursor.execute('SELECT pg_current_snapshot()::text AS snapshot');snapshot=cursor.fetchone()['snapshot']
                cursor.execute(COUNT_SQL,(project_id,state));count=cursor.fetchone()['count']
                cursor.execute(RANK_SQL[query_kind],(project_id,state,limit,offset));issues=cursor.fetchall()
        return {'read_operation':OPERATION,'snapshot':snapshot,'issues':issues,'issuesCount':count,
            'project_id':project_id,'state':state,'limit':limit,'offset':offset}

    def ranked(self,project_id=0,state='open',limit=20,offset=0):
        config=self.configuration()
        with self.driver.connect(self.dsn,autocommit=True) as conn:
            conn.execute(self.driver.sql.SQL('SET search_path TO {}, public').format(self.driver.sql.Identifier(self.schema)))
            return self.ranked_on(conn,config['query_kind'],project_id,state,limit,offset)

    def ranking_diagnostics(self):
        config=self.configuration()
        with self.connection() as conn:
            # Execute the plan to retain rows/loops/buffers, without elapsed-time fields.
            plan=conn.execute('EXPLAIN (ANALYZE,BUFFERS,TIMING OFF,SUMMARY OFF,FORMAT JSON) '+RANK_SQL[config['query_kind']],(0,'open',20,0)).fetchone()[0]
            indexes=conn.execute('SELECT indexname,indexdef FROM pg_indexes WHERE schemaname=%s ORDER BY indexname',(self.schema,)).fetchall()
            return {'read_operation':OPERATION,'query_kind':config['query_kind'],
                'read_query_sha256':hashlib.sha256((COUNT_SQL+'\n'+RANK_SQL[config['query_kind']]).encode()).hexdigest(),
                'rank_plan':plan,'indexes':indexes,'version':conn.execute('SELECT version()').fetchone()[0],
                'timing_estimates':False,'scope':'Small-fixture correctness and executed SQL plan evidence; no latency comparison.'}


class RankingOracle(IssueOracle):
    def expected_ranked(self,snapshot,project_id,state,limit=20,offset=0):
        parse_snapshot(snapshot);validate_page(project_id,state,limit,offset)
        active=tuple(r['operation_id'] for r in self.ledger if visible(r['transaction_xid'],snapshot))
        key=(OPERATION,active,project_id,state,limit,offset)
        if key not in self.cache:
            rows=[self.issue_at(i,snapshot) for i in range(self.rows) if i%5==project_id]
            rows=[r for r in rows if r['state']==state]
            rows.sort(key=lambda r:(-r['comment_count'],-r['id']))
            self.cache[key]={'issues':rows[offset:offset+limit],'issuesCount':len(rows)}
        return self.cache[key]

    def verify_ranked(self,evidence):
        if evidence.get('read_operation')!=OPERATION:raise ValueError('ranking operation identity differs')
        expected=self.expected_ranked(evidence['snapshot'],evidence['project_id'],evidence['state'],evidence['limit'],evidence['offset'])
        if type(evidence['issuesCount']) is not int or evidence['issuesCount']!=expected['issuesCount']:raise ValueError('ranking filtered count differs')
        keys=('id','project_id','project_name','title','state','revision','assignee','comment_count')
        actual=[{k:r[k] for k in keys} for r in evidence['issues']]
        if any(type(r[k]) is not int for r in actual for k in ('id','project_id','revision','comment_count')):raise ValueError('invalid ranking numeric field')
        if actual!=expected['issues']:raise ValueError('ranking order/content differs from independent snapshot')


class RankingService(IssueService):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.fixture=RankingFixture(self.fixture.dsn,self.fixture.schema)
        self.query_sha256=hashlib.sha256((COUNT_SQL+'\n'+RANK_SQL[self.config['query_kind']]).encode()).hexdigest()

    def page(self,project_id=0,state='open',limit=20,offset=0):
        # Keep correctness-stage response free of new comparative timing fields.
        with self.pool.acquire() as (conn,_):
            return self.fixture.ranked_on(conn,self.config['query_kind'],project_id,state,limit,offset)

    def diagnostics(self):
        return self.fixture.ranking_diagnostics()
