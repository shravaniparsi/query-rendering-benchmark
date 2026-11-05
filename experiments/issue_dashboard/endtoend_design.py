"""Finite separate-host development bridge after the native distribution control."""
import itertools
import random
from .distribution import FAMILIES
from .mechanism_design import load_plan
from .ranking_design import ORDERED, FILTER_FIRST

VERSION = 'endtoend-distribution-v1.1'
ROLE = 'separate-host query-shape and rendering development bridge'
SEED = 202611061
CANDIDATES = (
    ('correlated', 'correlated', None), ('batched', 'batched', None),
    (ORDERED, 'maintained', ORDERED), (FILTER_FIRST, 'maintained', FILTER_FIRST))


def design(smoke=False):
    if type(smoke) is not bool: raise ValueError('boolean smoke required')
    return {'version': VERSION, 'rows': 100 if smoke else 100000, 'fanout': 5,
        'duration_s': 5 if smoke else 20, 'rates': [4] if smoke else [16, 64],
        'write_fractions': [.4] if smoke else [.02, .5], 'read_operation': 'discussion_rank',
        'placements': ['split'], 'strategies': ['ssr', 'csr'], 'candidates': [c[0] for c in CANDIDATES],
        'pool_size': 4, 'prepare_threshold': None, 'generator_workers': 16, 'max_dispatch_lag_s': .05,
        'drain_timeout_s': 30, 'warmup_reads': 4, 'serial_operations': 10 if smoke else 60,
        'serial_write_fraction': .4, 'serial_browser_duration_s': 5 if smoke else 10,
        'loaded_browser_offsets_s': [1, 2, 3] if smoke else [3, 9, 15],
        'loaded_browser_deadline_ms': 4000, 'loaded_browser_max_dispatch_lag_s': .1,
        'overhead': {'rows': 1000, 'fanout': 5, 'window_s': 5, 'paired_blocks': 3,
            'workers': 4, 'max_operations_per_window': 50000, 'minimum_context_median_ratio': .90},
        'target_deadline_s': 3300, 'target_idle_s': 600, 'http_policy': 'owned-http11-receipt-v2',
        'socket_idle_s': 2, 'socket_request_cap': 32, 'server_connection_limit': 32,
        'http_timeout_s': 5, 'gateway_upstream_timeout_s': 15, 'sql_timeout_s': 10, 'pool_admission_timeout_s': 2,
        'connection_modes': ['persistent'], 'socket_tcp_nodelay': True,
        'source_seed': SEED, 'confirmatory_data': False, 'selector_fit': False,
        'scope': 'Fixed split app/database CPUs; both renderers and every query candidate under two rates/two write fractions. No main/held-out inference.'}


def identities(allocation, block):
    if type(allocation) is not int or allocation not in range(3) or type(block) is not int or block not in range(3):
        raise ValueError('allocation/block must be 0,1,2')
    return FAMILIES[block]


def cases(allocation, block, smoke=False):
    family, seed = identities(allocation, block); spec = design(smoke); result = []
    for phase in ('qualification', 'serial', 'loaded'):
        workload = [(None, None)] if phase != 'loaded' else list(itertools.product(spec['rates'], spec['write_fractions']))
        random.Random(SEED + allocation * 100 + block * 10 + len(phase)).shuffle(workload)
        for rate, fraction in workload:
            choices = list(itertools.product(CANDIDATES, spec['strategies']))
            random.Random(SEED + allocation * 1000 + block * 100 + (rate or 0) + round((fraction or 0) * 100)).shuffle(choices)
            for (candidate, query, ranking), strategy in choices:
                result.append({'name': f'{phase}-{len([c for c in result if c["phase"] == phase])}',
                    'phase': phase, 'candidate': candidate, 'query_kind': query, 'ranking_design': ranking,
                    'strategy': strategy, 'read_operation': 'discussion_rank', 'family': family, 'seed': seed,
                    'rows': spec['overhead']['rows'] if phase == 'qualification' else spec['rows'],
                    'fanout': 5, 'rate': rate, 'write_fraction': fraction, 'placement': 'split'})
    return result


def trace(case, allocation, smoke=False):
    spec = design(smoke)
    if case['phase'] != 'loaded': raise ValueError('loaded context required')
    if smoke:
        from .arrivals import schedule
        return schedule(case['rate'], spec['duration_s'], case['write_fraction'], case['rows'], SEED + allocation)
    return load_plan(case['rate'], spec['duration_s'], case['write_fraction'], case['rows'], 'uniform', SEED + allocation, workers=16)


def qualification_order(allocation, block, index, pair):
    identities(allocation, block)
    if type(index) is not int or index not in range(8) or type(pair) is not int or pair not in range(3): raise ValueError('outside paired order')
    values = [False, True]; random.Random(SEED + allocation * 1000 + block * 100 + index * 10 + pair).shuffle(values)
    return values
