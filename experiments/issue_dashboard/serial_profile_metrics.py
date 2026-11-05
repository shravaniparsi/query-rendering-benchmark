"""Serial collection clocks and profiles derived only from the separate phase."""
import hashlib,json,math,statistics,time
from experiments.study_design.decision import validate_profile


def execute_serial(plan,operation,on_record=None):
    if on_record is not None and not callable(on_record):raise ValueError('record sink must be callable')
    records=[];start_ns=time.perf_counter_ns();start=start_ns/1e9
    for item in plan:
        dispatched=time.perf_counter()-start
        try:result=dict(operation(item))
        except Exception as error:result={'outcome':'failed','error_type':type(error).__name__}
        validated=time.perf_counter()-start;received=result.pop('_response_completed_monotonic',None)
        complete=validated if received is None else received-start
        records.append({**item,**result,'intended_s':dispatched,'dispatch_s':dispatched,'complete_s':complete,'validation_complete_s':validated})
        if on_record is not None:on_record(records)
    end_ns=time.perf_counter_ns()
    timing={'role':'sequential HTTP profile followed by quiet browser phase','http_start_ns':str(start_ns),'http_finish_ns':str(end_ns),
            'http_duration_s':(end_ns-start_ns)/1e9,'max_in_flight':1}
    verify_serial(records,plan,timing);return records,timing


def verify_serial(records,plan,timing):
    if len(records)!=len(plan) or timing['max_in_flight']!=1:raise ValueError('serial record budget differs')
    start,end=int(timing['http_start_ns']),int(timing['http_finish_ns']);duration=(end-start)/1e9
    if start<=0 or end<=start or duration!=timing['http_duration_s']:raise ValueError('serial phase clocks differ')
    previous=0
    for row,item in zip(records,plan):
        if any(row.get(k)!=v for k,v in item.items()) or row['outcome'] not in ('completed','failed'):raise ValueError('serial plan/outcome differs')
        times=[row[k] for k in ('intended_s','dispatch_s','complete_s','validation_complete_s')]
        if not all(type(v) in (float,int) and math.isfinite(v) for v in times):raise ValueError('serial clocks nonfinite')
        if not 0<=previous<=times[0]==times[1]<=times[2]<=times[3]<=duration:raise ValueError('overlapping or invalid serial request clocks')
        previous=times[-1]


def evidence_digest(index):return hashlib.sha256(json.dumps(index,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def derive_features(query,placement,source,selected,evidence_index,spec):
    """selected[strategy] supplies audited records/events/browser summary."""
    if set(selected)!={'ssr','csr'}:raise ValueError('both rendering profiles required')
    usable=all(all(r['outcome']=='completed' for r in v['records']) and not v['ambiguous_writes'] and v['browser_summary']['eligible'] for v in selected.values())
    if not usable:return {'query_kind':query,'placement':placement,'status':'unusable','envelope':None,'evidence_index':evidence_index,'reason':'HTTP, ambiguity or quiet-browser health/window failure'}
    values={k:[] for k in ('read','comment','state')};result_rows=[];payloads=[];steps={}
    for strategy,value in selected.items():
        by_id={e['operation_id']:e for e in value['events']};prefix=value['case_name'];step_counts=[]
        for row in value['records']:
            event=by_id[f'{prefix}-{row["id"]}'];kind='read' if row['kind']=='read' else row['mutation'];native=event['backend']['transaction_wall_ms']
            if not math.isfinite(native) or native<=0:raise ValueError('positive native transaction-wall profile required')
            values[kind].append(native)
            if kind=='read':
                result_rows.append(len(row['read_evidence']['issues']));step_counts.append(len(row['steps']))
                if strategy=='csr':payloads.append(row['steps'][-1]['bytes'])
        expected=1 if strategy=='ssr' else 2
        if not step_counts or set(step_counts)!={expected}:raise ValueError('profile logical request graph differs')
        steps[strategy]=expected
    if any(len(v)<2 for v in values.values()) or not payloads:raise ValueError('profile sample classes incomplete')
    features={'read_service_median_ms':statistics.median(values['read']),
        'read_service_cv':statistics.stdev(values['read'])/statistics.mean(values['read']),
        'write_a_service_median_ms':statistics.median(values['comment']),'write_b_service_median_ms':statistics.median(values['state']),
        'ssr_serial_paint_median_ms':selected['ssr']['browser_summary']['descriptive']['critical_content_paint_ms']['median'],
        'csr_serial_paint_median_ms':selected['csr']['browser_summary']['descriptive']['critical_content_paint_ms']['median'],
        'read_payload_bytes':statistics.median(payloads),'target_logical_cpus':2,'db_logical_cpus':2 if placement=='shared' else 1,
        'pool_size':spec['pool_size'],'result_rows':statistics.median(result_rows),'fixture_rows':spec['rows'],
        'ssr_request_count':steps['ssr'],'csr_request_count':steps['csr']}
    envelope={'application':'issue-dashboard','query_candidate':query,'phase':'permitted_serial_profile','source_commit':source,
              'evidence_sha256':evidence_digest(evidence_index),'features':features}
    validate_profile(envelope)
    return {'query_kind':query,'placement':placement,'status':'usable','envelope':envelope,'evidence_index':evidence_index,
        'native_samples':{k:len(v) for k,v in values.items()},'meaning':'Native transaction wall is service-profile elapsed time, not query CPU or isolated maintenance. Paint medians use three quiet probes per path.'}
