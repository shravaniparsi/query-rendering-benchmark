"""Prospective off-host resource and instrumentation development diagnostic."""
import itertools
import random

ROLE='off-host resource and WAL development diagnostic'
SEED_BASE=202611120


def design(smoke=False):
    return {'rows':1000 if smoke else 10000,'fanout':20 if smoke else 200,'duration_s':5 if smoke else 20,
        'rate':4 if smoke else 128,'warmup_s':2 if smoke else 10,'warmup_rate':4,'write_fraction':.2,
        'query_kinds':['correlated','maintained'],'strategies':['ssr','csr'],
        'placements':['shared','split'],'instrumented':[False,True],'pool_size':4,'generator_workers':64,
        'drain_timeout_s':30,'prepare_threshold':None,'completion_screen':.99,'analysis_unit':'paired VM allocation',
        'browser_timing_estimates':False,'confirmatory_data':False}


def cases_for(allocation,smoke=False):
    if allocation not in (0,1,2):raise ValueError('three allocation identities only')
    spec=design(smoke)
    rows=[dict(zip(('query_kind','strategy','placement','instrumented'),v)) for v in itertools.product(
        spec['query_kinds'],spec['strategies'],spec['placements'],spec['instrumented'])]
    random.Random(SEED_BASE+allocation).shuffle(rows)
    return [{**r,'name':f'cell-{i}'} for i,r in enumerate(rows)]
