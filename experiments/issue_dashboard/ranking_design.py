"""Explicit maintained-ranking read designs with identical storage and writes."""
import hashlib
import inspect
import json

from .distribution import DistributionFixture, DistributionRankingService
from .fixture import COUNT_SQL, FIELDS, IssueFixture, validate_page
from .ranking import RANK_SQL

VERSION = 'issue-ranking-design-v1'
ORDERED = 'maintained-ordered-v1'
FILTER_FIRST = 'maintained-filter-first-v0'
DESIGNS = (ORDERED, FILTER_FIRST)
FILTER_FIRST_SQL = '''WITH filtered AS MATERIALIZED (
 SELECT id,project_id,assignee_id,title,state,revision FROM issues
 WHERE project_id=%s AND state=%s)
 SELECT ''' + FIELDS + ''' s.comment_count
 FROM filtered p JOIN issue_counts s ON s.issue_id=p.id
 JOIN assignees u ON u.id=p.assignee_id JOIN projects pr ON pr.id=p.project_id
 ORDER BY s.comment_count DESC,p.id DESC LIMIT %s OFFSET %s'''


def read_sql(design):
    if design == ORDERED: return RANK_SQL['maintained']
    if design == FILTER_FIRST: return FILTER_FIRST_SQL
    raise ValueError('unknown maintained ranking design')


def design_metadata(design):
    return {'version': VERSION, 'design': design, 'write_query_kind': 'maintained',
        'read_query_sha256': hashlib.sha256((COUNT_SQL + '\n' + read_sql(design)).encode()).hexdigest(),
        'write_method_sha256': hashlib.sha256(inspect.getsource(IssueFixture.write_on).encode()).hexdigest()}


def validate_design(metadata):
    if not isinstance(metadata, dict) or metadata != design_metadata(metadata.get('design')):
        raise ValueError('maintained ranking design identity differs')
    return metadata


def normalized_indexes(indexes, schema):
    return [[name, definition.replace(' ON ' + schema + '.', ' ON __schema__.')] for name, definition in indexes]


class RankingDesignFixture(DistributionFixture):
    def initialize(self, rows=1000, fanout=20, query_kind='maintained', *, distribution_family='modulo-v1',
                   distribution_seed=None, read_operation='discussion_rank', ranking_design=ORDERED):
        if query_kind != 'maintained' or read_operation != 'discussion_rank':
            raise ValueError('ranking design requires maintained discussion ranking')
        metadata = design_metadata(ranking_design)
        super().initialize(rows, fanout, query_kind, distribution_family=distribution_family,
            distribution_seed=distribution_seed, read_operation=read_operation)
        with self.connection() as conn:
            conn.execute('CREATE TABLE ranking_design_metadata (configuration jsonb NOT NULL)')
            conn.execute('INSERT INTO ranking_design_metadata VALUES (%s::jsonb)', (json.dumps(metadata),))
        self._ranking_design = metadata

    def configuration(self):
        config = super().configuration()
        with self.connection() as conn:
            values = conn.execute('SELECT configuration FROM ranking_design_metadata').fetchall()
        if len(values) != 1: raise ValueError('ranking design metadata requires exactly one row')
        self._ranking_design = validate_design(values[0][0])
        return {**config, 'ranking_design': self._ranking_design}

    def select_design(self, design):
        """Inspection-only metadata transition between completed read contexts."""
        metadata = design_metadata(design); self.configuration()
        with self.connection() as conn:
            conn.execute('UPDATE ranking_design_metadata SET configuration=%s::jsonb', (json.dumps(metadata),))
        self._ranking_design = metadata

    def ranked_on(self, conn, query_kind, project_id=0, state='open', limit=20, offset=0):
        if query_kind != 'maintained': raise ValueError('ranking design requires maintained writes')
        validate_page(project_id, state, limit, offset)
        if not hasattr(self, '_ranking_design'): self.configuration()
        sql = read_sql(self._ranking_design['design'])
        from psycopg.rows import dict_row
        with conn.transaction():
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            snapshot = conn.execute('SELECT pg_current_snapshot()::text').fetchone()[0]
            count = conn.execute(COUNT_SQL, (project_id, state)).fetchone()[0]
            with conn.cursor(row_factory=dict_row) as cursor:
                cursor.execute(sql, (project_id, state, limit, offset)); issues = cursor.fetchall()
        return {'read_operation': 'discussion_rank', 'snapshot': snapshot, 'issues': issues, 'issuesCount': count,
                'project_id': project_id, 'state': state, 'limit': limit, 'offset': offset}

    def ranking_diagnostics(self):
        config = self.configuration()
        with self.connection() as conn:
            plan = conn.execute('EXPLAIN (ANALYZE,BUFFERS,TIMING OFF,SUMMARY OFF,FORMAT JSON) ' +
                read_sql(config['ranking_design']['design']), (0, 'open', 20, 0)).fetchone()[0]
            indexes = conn.execute('SELECT indexname,indexdef FROM pg_indexes WHERE schemaname=%s ORDER BY indexname', (self.schema,)).fetchall()
            return {'configuration': config, 'schema': self.schema, 'read_operation': 'discussion_rank',
                'read_query_sha256': config['ranking_design']['read_query_sha256'], 'read_plan': plan,
                'indexes': indexes, 'normalized_indexes': normalized_indexes(indexes, self.schema),
                'version': conn.execute('SELECT version()').fetchone()[0], 'timing_estimates': False}


class RankingDesignService(DistributionRankingService):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fixture = RankingDesignFixture(self.fixture.dsn, self.fixture.schema)
        self.config = self.fixture.configuration()
        self.query_sha256 = self.config['ranking_design']['read_query_sha256']
