"""Separate, one-in-flight service profiles and quiet browser probes."""
import itertools,random
from .offhost_loaded_design import design as loaded_design
from .arrivals import schedule

ROLE='off-host separate serial-profile development'
SEED_BASE=202611190


def design(smoke=False):
    value=loaded_design(smoke);value.pop('rates');value.pop('write_fraction');value.pop('generator_workers');value.pop('drain_timeout_s')
    value.update(query_kinds=['correlated','batched','maintained'],placements=['split'] if smoke else ['shared','split'],
        duration_s=10 if smoke else 16,loaded_browser_offsets_s=[1,4,7] if smoke else [1,6,11],
        loaded_browser_deadline_ms=2000 if smoke else 4000,warmup_s=2,warmup_rate=4,
        serial_operations=10 if smoke else 60,serial_write_fraction=.4,serial_max_in_flight=1,schedule_seed_base=SEED_BASE,
        target_deadline_s=1500,target_idle_s=600,profiling_phase='permitted_serial_profile',
        scope='One in-flight logical request at a time, then three quiet fresh-context browser probes; fresh fixtures separate this phase from loaded evaluation.')
    return value


def cases_for(allocation,smoke=False):
    if type(allocation) is not int or allocation not in (0,1,2):raise ValueError('three allocation identities required')
    spec=design(smoke);rng=random.Random(SEED_BASE+allocation);placements=list(spec['placements']);rng.shuffle(placements);rows=[]
    for placement in placements:
        choices=list(itertools.product(spec['query_kinds'],spec['strategies']));rng.shuffle(choices)
        rows.extend({'query_kind':q,'strategy':s,'placement':placement,'instrumented':False} for q,s in choices)
    return [{**row,'name':f'serial-cell-{i}'} for i,row in enumerate(rows)]


def serial_plan(spec,allocation):
    rows=schedule(1,spec['serial_operations'],spec['serial_write_fraction'],spec['rows'],SEED_BASE+allocation)
    return [{**{k:v for k,v in row.items() if k!='at_s'},'profile_order':i} for i,row in enumerate(rows)]
