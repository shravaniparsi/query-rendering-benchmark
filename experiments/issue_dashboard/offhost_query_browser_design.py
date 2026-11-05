"""Matched all-query loaded-browser development panels."""
import itertools,random
from .offhost_loaded_design import design as loaded_design

ROLE='off-host all-query sparse-browser development panels'
SEED_BASE=202611180


def design(smoke=False):
    value=loaded_design(smoke);value.update(query_kinds=['correlated','batched','maintained'],
        placements=['split'] if smoke else ['shared','split'],rates=[4] if smoke else [32,128],
        schedule_seed_base=SEED_BASE,target_deadline_s=2100,
        scope='Complete matched query/rendering development panels with sparse loaded browser probes; no holdout or main inference.')
    return value


def cases_for(allocation,smoke=False):
    if type(allocation) is not int or allocation not in (0,1,2):raise ValueError('three allocation identities required')
    spec=design(smoke);rng=random.Random(SEED_BASE+allocation);blocks=list(itertools.product(spec['placements'],spec['rates']));rng.shuffle(blocks);rows=[]
    for placement,rate in blocks:
        choices=list(itertools.product(spec['query_kinds'],spec['strategies']));rng.shuffle(choices)
        rows.extend({'query_kind':q,'strategy':s,'placement':placement,'instrumented':False,'rate':rate} for q,s in choices)
    return [{**row,'name':f'query-cell-{i}'} for i,row in enumerate(rows)]
