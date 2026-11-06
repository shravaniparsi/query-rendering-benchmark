"""Prospective, bounded daily cells and matched traces for reserved evaluation."""
from datetime import date
import random
import itertools
from .inventory import require
from .workload import APPLICATIONS,trace,VERSION

SEED=202611061


def index_for(artifacts,day):
    dates=artifacts['sample_plan']['collection_dates_utc']
    if day in dates:return dates.index(day)
    require(day in artifacts['sample_plan'].get('qualification_dates_utc',[]),'unplanned calendar date');return 0


def rotate(values,index):
    values=list(values);n=index%len(values);return values[n:]+values[:n]


def cells(artifacts,task,day,smoke=False):
    require(task in APPLICATIONS and type(smoke) is bool,'reserved task/smoke identity required');index=index_for(artifacts,day);app=APPLICATIONS[task];contexts=artifacts['main_contexts']
    selected=sorted([r for r in contexts['contexts'] if r['application']==app],key=lambda r:r['id']);catalog=sorted(contexts['catalogs'][app]);spec=artifacts['profile_protocol']
    require(spec['version']=='permitted-serial-profile-v2' and spec['workload_trace_version']==VERSION and spec['serial']['operations_per_renderer']==60 and spec['serial']['quiet_probes_per_renderer']==3,'fixed serial/trace protocol required')
    require(spec['overhead']['rows']==1000 and spec['overhead']['window_s']==5 and spec['overhead']['pairs']==3 and spec['overhead']['max_operations_per_window']==50000 and spec['overhead']['minimum_context_median_ratio']==.90,'fixed observation qualification required')
    require(len(selected)==8 and len({r['id'] for r in selected})==8 and
        {(r['rows'],r['offered_rate'],r['nominal_write_fraction']) for r in selected}==set(itertools.product((1000,100000),(16,64),(.02,.5))) and
        set(catalog)=={q+'/'+r for q,r in itertools.product(('correlated','batched','maintained'),('ssr','csr'))} and
        len(catalog)==6 and all(r['duration_s']==20 for r in selected),'fixed main context grid required')
    mode=artifacts['sample_plan'].get('qualification_date_mode','complete_profile_and_loaded_smoke')
    require(mode in ('complete_profile_and_loaded_smoke','complete_profile_and_loaded_full_panel'),'unknown qualification coverage')
    small=smoke and mode=='complete_profile_and_loaded_smoke';values=[]
    def add(phase,config,rows,context=None):
        candidate,strategy=config.rsplit('/',1);row={'name':phase[0]+'-'+str(sum(v['phase']==phase for v in values)).zfill(3),'phase':phase,'task':task,'application':app,
            'rows':rows,'candidate':candidate,'query_kind':candidate,'strategy':strategy,'placement':'split','collection_date_utc':day,'smoke':smoke,
            'context_id':None if context is None else context['id'],'read_operation':'inventory_page' if task=='inventory' else 'event_time_groups'}
        if context:row.update(offered_rate=context['offered_rate'],nominal_write_fraction=context['nominal_write_fraction'],duration_s=context['duration_s'])
        values.append(row)
    base=catalog.copy();random.Random(SEED+(0 if task=='inventory' else 1)).shuffle(base)
    for config in rotate(base,index):add('qualification',config,1000)
    for rows in rotate([1000,100000],index):
        for config in rotate(base,index+(rows==100000)):add('serial',config,rows)
    order=selected.copy();random.Random(SEED+(100 if task=='inventory' else 101)).shuffle(order)
    for context in rotate(order,index):
        if small and context['rows']!=1000:continue
        for config in rotate(base,index+selected.index(context)):add('loaded',config,context['rows'],context)
    require(len(values)==(42 if small else 66),'declared case count differs');return values


def trace_for(artifacts,case):
    ordinal=date.fromisoformat(case['collection_date_utc']).toordinal();task=case['task']
    if case['phase']=='serial':return trace(task,case['rows'],1,60,.4,SEED+ordinal*17+case['rows'])
    require(case['phase']=='loaded','workload trace only for serial/loaded cells')
    selected=sorted([r for r in artifacts['main_contexts']['contexts'] if r['application']==case['application']],key=lambda r:r['id'])
    row=next(r for r in selected if r['id']==case['context_id']);require(case['rows']==row['rows'] and case['offered_rate']==row['offered_rate'] and case['nominal_write_fraction']==row['nominal_write_fraction'],'actual case differs from fixed context')
    return trace(task,case['rows'],row['offered_rate'],20,row['nominal_write_fraction'],SEED+ordinal*17+selected.index(row)*101+(0 if task=='inventory' else 1))


def qualification_order(day,case_index,pair):
    result=[False,True];random.Random(SEED+date.fromisoformat(day).toordinal()*31+case_index*7+pair).shuffle(result);return result
