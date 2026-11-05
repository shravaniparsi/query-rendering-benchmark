"""Fixed baseline load ladder for short-window off-host feasibility calibration."""
import itertools
import random

from .offhost_resource_design import design as resource_design

ROLE='off-host baseline load calibration'
SEED_BASE=202611150


def design(smoke=False):
    value=resource_design(smoke)
    del value['rate']
    value.update(query_kinds=['correlated'],instrumented=[False],rates=[2,4] if smoke else [4,16,32,64,128,256],
        schedule_seed_base=SEED_BASE,target_deadline_s=1650,
        calibration_scope='Indexed correlated-query baseline; logical HTTP delivery with browser probes outside load.',
        main_load_selected=False)
    return value


def cases_for(allocation,smoke=False):
    if type(allocation) is not int or allocation not in (0,1,2):raise ValueError('three allocation identities required')
    spec=design(smoke);blocks=list(itertools.product(spec['strategies'],spec['placements']))
    random.Random(SEED_BASE+allocation).shuffle(blocks)
    rows=[{'query_kind':'correlated','strategy':strategy,'placement':placement,'instrumented':False,'rate':rate}
          for strategy,placement in blocks for rate in spec['rates']]
    return [{**row,'name':f'cell-{i}'} for i,row in enumerate(rows)]
