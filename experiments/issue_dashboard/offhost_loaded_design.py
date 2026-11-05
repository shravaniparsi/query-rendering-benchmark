"""Sparse browser probes embedded in a fixed, calibrated HTTP load range."""
import itertools
import random

from .offhost_resource_design import design as resource_design

ROLE='off-host sparse browser probes during logical load'
SEED_BASE=202611160


def design(smoke=False):
    value=resource_design(smoke);del value['rate']
    value.update(query_kinds=['correlated'],instrumented=[False],rates=[4] if smoke else [32,64,128],
        duration_s=10 if smoke else 20,schedule_seed_base=SEED_BASE,target_deadline_s=1500,
        loaded_browser_offsets_s=[1,4,7] if smoke else [2,7,12],
        loaded_browser_deadline_ms=2000 if smoke else 4000,loaded_browser_max_dispatch_lag_s=.2,
        browser_timing_estimates=True,browser_population=False,main_load_selected=False,
        scope='Three sparse cold-context browser probes per HTTP load window; development feasibility, not a browser population or INP.')
    return value


def cases_for(allocation,smoke=False):
    if type(allocation) is not int or allocation not in (0,1,2):raise ValueError('three allocation identities required')
    spec=design(smoke);blocks=list(itertools.product(spec['strategies'],spec['placements']))
    random.Random(SEED_BASE+allocation).shuffle(blocks)
    rows=[{'query_kind':'correlated','strategy':strategy,'placement':placement,'instrumented':False,'rate':rate}
          for strategy,placement in blocks for rate in spec['rates']]
    return [{**row,'name':f'cell-{i}'} for i,row in enumerate(rows)]
