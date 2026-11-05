"""Prospective development budgets; this does not release a main policy experiment."""
import itertools,random
from .arrivals import schedule,STATES

SEED=202611021
OPERATIONS=('recent','discussion_rank')
QUERIES=('correlated','batched','maintained')


def design(smoke=False):
    return {'version':'native-scale-profile-v1','data_role':'development only; no fitted selector/main/heldout release',
        'operations':list(OPERATIONS),'query_kinds':list(QUERIES),'rows':[100] if smoke else [1000,10000,100000],
        'fanouts':[5] if smoke else [5,50],'profile_operations':60,'profile_write_fraction':.4,'warmup_reads':4,
        'pool_size':4,'prepare_threshold':None,'native_max_in_flight':1,'statement_timeout_s':10,
        'overhead':{'rows':1000,'fanout':20,'paired_blocks':3,'window_s':2,'max_operations':50000,
            'periodic_sampler_s':1,'minimum_context_median_on_off_throughput_ratio':.90},
        'cpu_placement':'controller/native clients and sampler on first selected CPU; PostgreSQL container on second',
        'native_scope':'SQL transaction elapsed including decode and commit; excludes pool wait, evidence hashing, validation and fixture setup',
        'future_load_domain':{'write_fractions':[.02,.2,.5],'workers':[1,4,16],'offered_rates':[1,4,16,64,256],
            'write_localities':['uniform','hot_comments'],'minimum_duration_s':20,'minimum_operations':100,'main_released':False,
            'scope':'Trace/calibration domain only; no full cross-product or HTTP/browser timing acquisition released'}}


def cases(allocation,smoke=False):
    if type(allocation) is not int or allocation not in (0,1,2):raise ValueError('allocation must be 0,1,2')
    spec=design(smoke);values=list(itertools.product(spec['operations'],spec['query_kinds'],spec['rows'],spec['fanouts']))
    random.Random(SEED+allocation).shuffle(values)
    return [{'name':f'native-{i}','read_operation':op,'query_kind':q,'rows':rows,'fanout':fanout} for i,(op,q,rows,fanout) in enumerate(values)]


def native_plan(case,allocation):
    seed=SEED+allocation
    return [{k:v for k,v in r.items() if k!='at_s'} for r in schedule(1,60,.4,case['rows'],seed)]


def overhead_cases(allocation):
    values=list(itertools.product(OPERATIONS,QUERIES));random.Random(SEED+100+allocation).shuffle(values)
    return [{'name':f'overhead-{i}','read_operation':op,'query_kind':q,'rows':1000,'fanout':20} for i,(op,q) in enumerate(values)]


def overhead_order(allocation,index,block):
    values=[False,True];random.Random(SEED+allocation*1000+index*10+block).shuffle(values);return values


def load_plan(rate,duration,write_fraction,rows,locality,seed,*,workers=16):
    """Comments may contend; state writes use distinct odd IDs and always change state."""
    domain=design()['future_load_domain']
    if type(workers) is not int or workers not in domain['workers']:raise ValueError('outside frozen worker ceilings')
    if type(rate) is not int or rate not in domain['offered_rates'] or duration!=max(20,100/rate) or write_fraction not in domain['write_fractions'] or locality not in domain['write_localities']:
        raise ValueError('outside frozen calibration trace domain')
    if type(rows) is not int or rows<1000 or rows>100000:raise ValueError('fixture bounds differ')
    values=schedule(rate,duration,write_fraction,rows,seed);rng=random.Random(seed+1)
    states=[r for r in values if r['kind']=='write' and r['mutation']=='state'];odd=list(range(1,rows,2))
    if len(states)>len(odd):raise ValueError('unique state-write capacity exceeded')
    state_ids=iter(rng.sample(odd,len(states)));even=list(range(0,rows,2));hot=comment_hotset(rows)
    for row in values:
        if row['kind']!='write':continue
        if row['mutation']=='state':
            issue_id=next(state_ids);row.update(issue_id=issue_id,value=STATES[((issue_id//5)%3+1)%3])
        else:row['issue_id']=rng.choice(even if locality=='uniform' else hot)
    return values


def comment_hotset(rows):
    rng=random.Random(SEED+rows);groups={}
    for issue_id in range(0,rows,2):groups.setdefault((issue_id%5,STATES[(issue_id//5)%3]),[]).append(issue_id)
    for values in groups.values():rng.shuffle(values)
    result=[];keys=sorted(groups)
    for i in range(max(len(keys),(rows//2)//100)):result.append(groups[keys[i%len(keys)]].pop())
    return sorted(result)
