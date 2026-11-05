"""Versioned count-distribution control; legacy fixtures and SQL remain unchanged."""
from collections import Counter
import hashlib
import json

from .fixture import IssueFixture, KINDS
from .ranking import RankingFixture, RankingOracle, RankingService
from .service import IssueService

VERSION = 'issue-count-distribution-v1'
STATE_FORMAT = 'issue-distribution-state-v1'
SEEDS = (202611031, 202611032)
FAMILIES = (('modulo-v1', None), *(('permuted-v1', seed) for seed in SEEDS))


def dimensions(rows, fanout):
    if type(rows) is not int or not 100 <= rows <= 100000 or type(fanout) is not int or not 0 <= fanout <= 200:
        raise ValueError('invalid distribution dimensions')


def initial_counts(rows, fanout, family, seed):
    dimensions(rows, fanout)
    if (family, seed) not in FAMILIES or (seed is not None and type(seed) is not int):
        raise ValueError('unknown distribution family/seed')
    source = list(range(rows))
    if family == 'permuted-v1':
        # Hash ordering defines a portable permutation, without Python RNG state.
        source.sort(key=lambda i: (hashlib.sha256(f'{VERSION}:{seed}:{rows}:{fanout}:{i}'.encode('ascii')).digest(), i))
    return [i % (fanout + 1) for i in source]


def describe(counts, fanout):
    histogram = Counter(counts)
    return {
        'initial_map_sha256': hashlib.sha256(''.join(f'{i}:{count}\n' for i, count in enumerate(counts)).encode('ascii')).hexdigest(),
        'histogram_sha256': hashlib.sha256(''.join(f'{count}:{histogram[count]}\n' for count in range(fanout + 1)).encode('ascii')).hexdigest(),
        'seeded_comment_total': sum(counts),
    }


class DistributionFixture(RankingFixture):
    def initialize(self, rows=1000, fanout=20, query_kind='correlated', *,
                   distribution_family='modulo-v1', distribution_seed=None, read_operation='discussion_rank'):
        if query_kind not in KINDS or read_operation not in ('recent', 'discussion_rank'):
            raise ValueError('unknown distribution query/operation')
        counts = initial_counts(rows, fanout, distribution_family, distribution_seed)
        metadata = {'version': VERSION, 'family': distribution_family, 'seed': distribution_seed,
                    'algorithm': 'identity' if distribution_family == 'modulo-v1' else 'sha256-rank-v1', **describe(counts, fanout)}
        # Both families take the same fresh legacy-schema/reseed path. Counters
        # and their indexes are built once, after the final comments are loaded.
        IssueFixture.initialize(self, rows, fanout, 'correlated')
        with self.connection() as conn:
            conn.execute('TRUNCATE comments')
            with conn.cursor().copy('COPY comments (id,issue_id,body) FROM STDIN') as copy:
                for issue_id, count in enumerate(counts):
                    for ordinal in range(count):
                        copy.write_row((issue_id * (fanout + 1) + ordinal, issue_id, f'Seed discussion {ordinal}'))
            conn.execute("SELECT setval(pg_get_serial_sequence('comments','id'),%s,true)", (rows * (fanout + 1),))
            conn.execute('UPDATE fixture_metadata SET query_kind=%s', (query_kind,))
            conn.execute('CREATE TABLE distribution_metadata (configuration jsonb NOT NULL,read_operation text NOT NULL)')
            conn.execute('INSERT INTO distribution_metadata VALUES (%s::jsonb,%s)', (json.dumps(metadata), read_operation))
            if query_kind == 'maintained':
                conn.execute('''CREATE TABLE issue_counts (issue_id integer PRIMARY KEY REFERENCES issues,
                    comment_count bigint NOT NULL CHECK(comment_count>=0))''')
                conn.execute('''INSERT INTO issue_counts SELECT i.id,count(c.id) FROM issues i
                    LEFT JOIN comments c ON c.issue_id=i.id GROUP BY i.id''')
                if read_operation == 'discussion_rank':
                    conn.execute('CREATE INDEX issue_counts_rank ON issue_counts(comment_count DESC,issue_id DESC)')

    def configuration(self):
        value = IssueFixture.configuration(self)
        with self.connection() as conn:
            rows = conn.execute('SELECT configuration,read_operation FROM distribution_metadata').fetchall()
        if len(rows) != 1:
            raise ValueError('distribution metadata must have exactly one row')
        return {**value, 'distribution': rows[0][0], 'read_operation': rows[0][1]}

    def validation_state(self):
        value = IssueFixture.validation_state(self)
        # Seed comment IDs stay below the reserved range, including after writes.
        with self.connection() as conn:
            seeded = conn.execute('''SELECT i.id,count(c.id) FROM issues i LEFT JOIN comments c
                ON c.issue_id=i.id AND c.id<=%s GROUP BY i.id ORDER BY i.id''',
                (value['rows'] * (value['fanout'] + 1),)).fetchall()
        if [row[0] for row in seeded] != list(range(value['rows'])):
            raise ValueError('initial distribution issue coverage differs')
        return {**value, 'format': STATE_FORMAT,
                'observed_initial_distribution': describe([row[1] for row in seeded], value['fanout'])}


class DistributionOracle(RankingOracle):
    def __init__(self, state):
        dimensions(state['rows'], state['fanout'])
        metadata = state['distribution']
        if state['format'] != STATE_FORMAT or state['read_operation'] not in ('recent', 'discussion_rank'):
            raise ValueError('unknown versioned distribution state')
        family, seed = metadata['family'], metadata['seed']
        if (family, seed) not in FAMILIES or (seed is not None and type(seed) is not int):
            raise ValueError('unknown distribution family/seed')
        # Independent construction: rank explicit (digest, source-ID) pairs.
        # This does not call the fixture's initial_counts/describe functions.
        pairs = []
        for source_id in range(state['rows']):
            token = f'issue-count-distribution-v1:{seed}:{state["rows"]}:{state["fanout"]}:{source_id}'
            pairs.append((hashlib.sha256(token.encode('ascii')).hexdigest(), source_id))
        order = range(state['rows']) if family == 'modulo-v1' else [row[1] for row in sorted(pairs)]
        self.initial = tuple(source_id % (state['fanout'] + 1) for source_id in order)
        frequencies = [0] * (state['fanout'] + 1)
        for count in self.initial:
            frequencies[count] += 1
        observed = {
            'initial_map_sha256': hashlib.sha256(b''.join(str(i).encode() + b':' + str(n).encode() + b'\n' for i, n in enumerate(self.initial))).hexdigest(),
            'histogram_sha256': hashlib.sha256(b''.join(str(i).encode() + b':' + str(n).encode() + b'\n' for i, n in enumerate(frequencies))).hexdigest(),
            'seeded_comment_total': sum(self.initial),
        }
        expected = {'version': VERSION, 'family': family, 'seed': seed,
                    'algorithm': 'identity' if family == 'modulo-v1' else 'sha256-rank-v1', **observed}
        if metadata != expected or state['observed_initial_distribution'] != observed:
            raise ValueError('distribution seed/map/histogram differs from independent reconstruction')
        self.distribution, self.operation = expected, state['read_operation']
        super().__init__(state)

    def issue_at(self, issue_id, snapshot=None):
        value = super().issue_at(issue_id, snapshot)
        value['comment_count'] += self.initial[issue_id] - issue_id % (self.fanout + 1)
        return value

    def verify_final(self, state):
        if state['format'] != STATE_FORMAT or state['distribution'] != self.distribution or state['read_operation'] != self.operation:
            raise ValueError('final distribution identity differs')
        DistributionOracle(state)
        return super().verify_final(state)


class DistributionRecentService(IssueService):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fixture = DistributionFixture(self.fixture.dsn, self.fixture.schema)
        self.config = self.fixture.configuration()

    def page(self, project_id=0, state='open', limit=20, offset=0):
        with self.pool.acquire() as (conn, _):
            return self.fixture.page_on(conn, self.config['query_kind'], project_id, state, limit, offset)


class DistributionRankingService(RankingService):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fixture = DistributionFixture(self.fixture.dsn, self.fixture.schema)
        self.config = self.fixture.configuration()
