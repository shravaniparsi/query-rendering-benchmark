"""Within-request phase budgets and explicitly selected successful-write tails."""
from collections import defaultdict
import math

from .write_phases import expected_phases


def validate_trace(backend,query_kind,mutation):
    value=backend['write_phase_trace'];phases=value['phases'];wall=backend['transaction_wall_ms']
    if value['format']!='issue-write-phases-v1' or [p['name'] for p in phases]!=expected_phases(query_kind,mutation):
        raise ValueError('wrong successful unique-write phases')
    previous=0;total=0
    for phase in phases:
        start,end,duration=(phase[k] for k in ('start_ms','end_ms','wall_ms'))
        if not all(math.isfinite(v) for v in (start,end,duration)) or not previous<=start<=end<=wall+1e-5:
            raise ValueError('nonfinite/overlapping/outside phase clocks')
        if not math.isclose(duration,end-start,rel_tol=1e-7,abs_tol=1e-5):raise ValueError('phase duration differs')
        previous=end;total+=duration
    if not math.isclose(total,value['phase_sum_ms'],abs_tol=1e-5) or not math.isclose(wall-total,value['residual_ms'],abs_tol=1e-5):
        raise ValueError('transaction phase budget differs')
    if value['residual_ms'] < -1e-5:raise ValueError('negative transaction residual')


def joint_tail(rows,selector):
    if not rows:return {'samples':0,'tail_n':0,'selected_operation_ids':[]}
    ordered=sorted(rows,key=lambda r:(r[selector],r['operation_id']),reverse=True)
    selected=ordered[:math.ceil(.05*len(rows))]
    total=sum(r['backend']['transaction_wall_ms'] for r in selected);phases=defaultdict(float)
    for row in selected:
        for phase in row['backend']['write_phase_trace']['phases']:phases[phase['name']]+=phase['wall_ms']
    residual=sum(r['backend']['write_phase_trace']['residual_ms'] for r in selected)
    client=[r['delivery_wall_ms'] for r in selected]
    return {'samples':len(rows),'tail_n':len(selected),'selected_operation_ids':[r['operation_id'] for r in selected],
        'transaction_wall_sum_ms':total,'pool_wait_sum_ms':sum(r['backend']['pool_wait_ms'] for r in selected),
        'completed_delivery_wall_sum_ms':sum(client) if all(v is not None for v in client) else None,
        'phase_wall_sum_ms':dict(phases),'residual_sum_ms':residual,
        'phase_fraction_of_transaction_wall':{k:v/total for k,v in phases.items()} if total else {},
        'residual_fraction_of_transaction_wall':residual/total if total else None}


def phase_summary(records,events,prefix,query_kind,enabled):
    writes={f'{prefix}-{r["id"]}':r for r in records if r['kind']=='write'}
    groups={k:[] for k in ('comment','state')};traced=0
    for event in events:
        row=writes.get(event['operation_id'])
        if row is None or event['status']!=200 or event.get('backend') is None:continue
        backend=event['backend'];present='write_phase_trace' in backend
        if present!=enabled:raise ValueError('write phase tracing configuration differs')
        if not enabled:continue
        validate_trace(backend,query_kind,row['mutation']);traced+=1
        delivery=(row['complete_s']-row['intended_s'])*1000 if row['outcome']=='completed' else None
        groups[row['mutation']].append({'operation_id':event['operation_id'],'backend':backend,
            'backend_wall_ms':backend['transaction_wall_ms'],'delivery_wall_ms':delivery})
    summary={'enabled':enabled,'successful_backend_write_traces':traced,'mutations':{}}
    if enabled:
        for kind,rows in groups.items():
            summary['mutations'][kind]={'backend_tail':joint_tail(rows,'backend_wall_ms'),
                'completed_delivery_tail':joint_tail([r for r in rows if r['delivery_wall_ms'] is not None],'delivery_wall_ms')}
    return summary
