"""Per-mutation and backend diagnostics, distinct from end-to-end delivery."""
from collections import Counter

from experiments.query_rendering_load.calibrate import quantile,summarize


def verify_backend_coverage(events,required,allowed):
    counts=Counter(e['operation_id'] for e in events)
    if not set(counts).issubset(allowed) or any(v!=1 for v in counts.values()):
        raise ValueError('unexpected/duplicate backend identities')
    by_id={e['operation_id']:e for e in events}
    for operation_id,fields in required.items():
        if counts[operation_id]!=1 or any(by_id[operation_id].get(k)!=v for k,v in fields.items()):
            raise ValueError('required backend identity/status/snapshot differs: '+operation_id)
    return counts


def mutation_summaries(records,duration):
    return {kind:summarize([r for r in records if r.get('mutation')==kind],duration)
            for kind in ('comment','state')}


def backend_summaries(records,events,prefix):
    identities={f'{prefix}-{r["id"]}':'read' if r['kind']=='read' else r['mutation'] for r in records}
    result={}
    for kind in ('read','comment','state'):
        selected=[e for e in events if identities.get(e['operation_id'])==kind]
        ok=[e for e in selected if e['status']==200 and e.get('backend') is not None]
        result[kind]={'traced_requests':len(selected),'statuses':dict(Counter(str(e['status']) for e in selected)),
                      'successful_traced_requests':len(ok)}
        for field in ('pool_wait_ms','transaction_wall_ms'):
            values=[e['backend'][field] for e in ok]
            result[kind][field]={'p50':quantile(values,.5),'p95':quantile(values,.95)}
    return result
