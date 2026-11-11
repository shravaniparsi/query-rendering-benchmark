"""Replay business state and derive serial-only profiles without running a task."""
from collections import Counter,defaultdict
import hashlib
import json
import math
import statistics
from urllib.parse import urlsplit

from .inventory import require
from .scale_reference import InventoryScaleReference,AnalyticsScaleReference
from .transport import TaskHTML
from .visible import verify_visible
from .workload import APPLICATIONS
from experiments.study_design.decision import validate_profile
from experiments.issue_dashboard.endtoend_transport import POLICY


def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':')).encode()
def digest(value):return hashlib.sha256(canonical(value)).hexdigest()
def finite(value):return type(value) in (int,float) and math.isfinite(value)


def parameters(task,value=None):
    defaults=({'category':None,'brand':None,'min_price_minor':0,'max_price_minor':None,'available_only':True,'offset':0,'limit':20} if task=='inventory' else
        {'start_s':100,'end_s':200,'group_by':['region','category'],'region':None,'category':None,'offset':0,'limit':20})
    return {**defaults,**(value or {})}


def request_shape(task,payload):
    if task=='inventory':return {'product_id':None,'quantity':None,**{k:v for k,v in payload.items() if k!='operation_id'}}
    return {'sale_id':None,'region':None,'category':None,**payload}


def verify_document(task,strategy,html,evidence):
    page=TaskHTML();page.feed(html);initial=page.result(task,strategy)
    if initial is not None:
        expected=parameters(task,evidence['parameters'])
        require(initial['parameters']==expected and all(initial[k]==v for k,v in evidence.items() if k not in ('visible_text','parameters')),'actual SSR document differs from initial DOM')
    return True


def verify_operations(bundle,plan,prefix,task,reference,events,cpu,allowed,writes,acknowledged,*,serial=False,closed=False):
    rows=bundle['operations'];phase=bundle['phase'];sessions=bundle['sessions'];native=defaultdict(list);owners={};connections={}
    require(phase['workers_joined'] is True and phase['attempts_per_http_request']==1 and
        0<int(phase['start_monotonic_ns'])<=int(phase['finish_monotonic_ns']),'worker/phase clock integrity differs')
    require(len(sessions)==len({s['worker'] for s in sessions})<=phase['workers'],'distinct worker sessions required')
    for session in sessions:
        owners[session['worker']]=session['owner_thread'];counts=Counter()
        for receipt in session['records']:
            require(receipt['policy']==POLICY and receipt['persistent'] is True and receipt['attempts']==1 and receipt['allowed_cpus']==[cpu] and
                receipt['outcome'] in ('completed','failed'),'owned receipt policy, CPU, attempts or outcome differs')
            require(int(receipt['validation_finish_ns'])>0,'native receipt clock absent')
            if receipt['body_receipt_ns'] is not None:require(0<int(receipt['body_receipt_ns'])<=int(receipt['validation_finish_ns']),'receipt follows validation')
            if receipt['outcome']=='completed':require(receipt['http_version']==11 and receipt['server_connection_id'],'observed HTTP/1.1 required')
            cid=receipt['server_connection_id']
            if cid:
                require(cid not in connections or connections[cid]==session['worker'],'connection shared between owners')
                connections[cid]=session['worker'];counts[cid]+=1
            native[receipt['operation_id']].append(receipt)
        require(all(v<=32 for v in counts.values()),'client socket exceeded declared request cap')
    require(len(set(owners.values()))==len(owners),'worker thread ownership differs')
    require([r['id'] for r in rows]==list(range(len(rows))),'offered identities missing or duplicated')
    if plan is not None:require(len(rows)==len(plan) and all(all(r.get(k)==v for k,v in item.items()) for r,item in zip(rows,plan)),'offered plan differs')
    if closed:
        require(not phase['errors'] and not phase['operation_cap_reached'],'qualification errors or operation cap')
        for key,evidence in phase['evidence_exemplars'].items():require(digest(evidence)==key,'exemplar digest differs');reference.verify_query(evidence)
    start=int(phase['start_monotonic_ns'])/1e9;duration=(int(phase['finish_monotonic_ns'])-int(phase['start_monotonic_ns']))/1e9
    previous=0
    for row in rows:
        opid=prefix+'-'+str(row['id']);receipts=native.pop(opid,[])
        require(row['outcome'] in ('completed','failed','admission_drop','dispatch_lag_drop','drain_timeout'),'unknown or integrity-failed operation')
        if row['outcome'] in ('admission_drop','dispatch_lag_drop'):
            require(not serial and not closed and not receipts and row['observed_s']>=row['at_s'],'dropped operation dispatched')
            if row['outcome']=='dispatch_lag_drop':require(row['observed_s']-row['at_s']>phase['max_dispatch_lag_s'],'false dispatch-lag drop')
            continue
        allowed.add(opid)
        if row['kind']=='write':
            name='operation_id' if task=='inventory' else 'event_id';writes[opid]=request_shape(task,{**row['payload'],name:opid})
        require(row['worker'] in owners and all(finite(row[k]) for k in ('dispatch_s','complete_s','validation_complete_s')) and
            0<=row['dispatch_s']<=row['complete_s']<=row['validation_complete_s']<=duration+1e-6,'logical receipt clock differs')
        if serial:require(row['dispatch_s']>=previous,'serial requests overlap');previous=row['validation_complete_s']
        if not serial and not closed:require(row['intended_s']==row['at_s'] and row['dispatch_s']>=row['intended_s'],'loaded offered clock differs')
        require(receipts and len(row['steps'])==len(receipts),'logical/native receipt coverage differs')
        for step,receipt in zip(row['steps'],receipts):require(all(step[k]==receipt[k if k!='bytes' else 'body_bytes'] for k in ('status','bytes','path','server_connection_id','outcome')),'logical/native step differs')
        last=receipts[-1]
        if last['body_receipt_ns'] is not None:require(math.isclose(row['complete_s']+start,int(last['body_receipt_ns'])/1e9,abs_tol=1e-6,rel_tol=0),'logical finish is not body receipt')
        require(int(last['validation_finish_ns'])/1e9<=start+row['validation_complete_s']+1e-6,'native validation follows logical validation')
        if row['outcome']=='completed':
            event=events[opid];require(event['status']==200,'completed work lacks successful native response')
            if row['kind']=='read':
                evidence=phase['evidence_exemplars'][row['evidence_sha256']] if closed else row['read_evidence'];reference.verify_query(evidence)
                require(evidence['parameters']==parameters(task,row.get('parameters')),'returned read parameters differ from offered business query')
                require(event['path']=='/query' and event['snapshot']==evidence['snapshot'],'read/native snapshot differs')
            else:
                ack=row['acknowledgement'];name='operation_id' if task=='inventory' else 'event_id'
                require(event['path']=='/control/mutate' and ack.get(name)==opid and ack.get('changed') is True,'write acknowledgement differs');acknowledged[opid]={k:v for k,v in ack.items() if k not in ('ok','backend','request_id')}
        if closed:require(row['outcome']=='completed','qualification work incomplete')
    require(not native,'unassigned logical receipts')
    return rows


def verify_browser(report,config,task,reference,events,allowed,writes,acknowledged):
    require(report['status']=='completed' and report['task']==task and report['strategy']==config['strategy'] and len(report['probes'])==len(config['offsets_s'])==3,'browser protocol coverage differs')
    signal=report['start_signal'];start=int(signal['measurement_start_monotonic_ns']);end=start+round(config['duration_s']*1e9)
    finish=int(report['probes_finished_monotonic_ns']);release=int(report['release_monotonic_ns'])
    require(signal['duration_s']==config['duration_s'] and start<=finish<=release and release>=end,'browser phase clocks differ')
    scored=[]
    for index,(row,offset) in enumerate(zip(report['probes'],config['offsets_s'])):
        require(row['id']==index and row['operation_id']==config['prefix']+'-loaded-'+str(index) and row['intended_offset_s']==offset and row['strategy']==config['strategy'] and row['cold_context'] is True,'browser identity differs')
        dispatch=int(row['dispatch_monotonic_ns']);done=int(row['finished_monotonic_ns']);actual=(dispatch-start)/1e9
        require(start<=dispatch<=done<=finish and math.isclose(actual,row['actual_offset_s'],abs_tol=1e-9) and actual>=offset,'browser dispatch clocks differ')
        require(row['outcome'] in ('completed','failed','dispatch_lag_drop') and row['integrity_error'] is False,'browser semantic integrity failure')
        for request in row['requests']:
            if request['path'] not in ('/task/ssr','/api/task','/api/task/mutate'):continue
            identity=request['operation_id'];require(isinstance(identity,str) and identity,'browser native identity missing');allowed.add(identity)
            if request['path']=='/api/task/mutate':
                payload=json.loads(request['post_data']);name='operation_id' if task=='inventory' else 'event_id';require(payload[name]==identity,'browser offer/body identity differs');writes[identity]=request_shape(task,payload)
        for evidence in row['evidence']:
            verify_visible(evidence);reference.verify_query(evidence);require(parameters(task,evidence['parameters'])==parameters(task),'foreground read changed its declared query')
        snapshots=[]
        initial=events.get(row['operation_id']) if config['strategy']=='ssr' else None
        if initial and initial['status']==200:require(initial['path']=='/query','SSR native route differs');snapshots.append(initial['snapshot'])
        for request in row['requests']:
            native=events.get(request['operation_id'])
            if request['path']=='/api/task' and native and native['status']==200:require(native['path']=='/query','API native route differs');snapshots.append(native['snapshot'])
        require([v['snapshot'] for v in row['evidence']]==snapshots[:len(row['evidence'])],'rendered DOM snapshots differ from ordered native browser reads')
        for response in row['responses']:
            if response['path'] not in ('/api/task','/api/task/mutate'):continue
            event=events.get(response['operation_id'])
            if event is not None:require(event['status']==response['status'] and event['path']==('/query' if response['path']=='/api/task' else '/control/mutate'),'browser API/native status or route differs')
            else:
                value=response['json'];error=value.get('error') if isinstance(value,dict) else None
                require(response['operation_id'] in allowed and row['outcome']=='failed' and 400<=response['status']<=599 and isinstance(value,dict) and value.get('ok') is False and isinstance(error,dict)
                    and error.get('code') in ('origin_http_failure','origin_transport_failure') and error.get('upstream_status')==response['status'],
                    'missing native browser event lacks an explicit failed upstream response')
            if response['status']==200:
                value=response['json'];require(value['ok'] is True,'successful API lacks application result')
                if response['path']=='/api/task':reference.verify_query(value);require(value['snapshot']==event['snapshot'],'API/native snapshot differs')
                else:acknowledged[response['operation_id']]={k:v for k,v in value.items() if k not in ('ok','backend','request_id')}
        for action in row['actions']:
            payload,result=action['request'],action['result'];name='operation_id' if task=='inventory' else 'event_id';opid=payload[name]
            require(writes.get(opid)==request_shape(task,payload) and result.get('changed') is True,'trusted action differs');acknowledged[opid]={k:v for k,v in result.items() if k not in ('ok','backend','request_id')}
        qualified=False
        if row['outcome']=='completed':
            require(row['checks_passed'] is True and row['document_status']==200 and row['app_outcome']=='completed' and len(row['evidence'])==3 and len(row['actions'])==2 and
                row['api_requests']==int(config['strategy']=='csr')+2 and not any(row[k] for k in ('page_errors','request_failures','http_errors','response_errors')),'completed browser workflow differs')
            require(row['trusted_clicks']==[{'id':'primary-action','trusted':True},{'id':'secondary-action','trusted':True}] and row['protocols'] and set(row['protocols'])=={'http/1.1'},'trusted click or HTTP protocol differs')
            verify_document(task,config['strategy'],row['document_html'],row['evidence'][0])
            text=row['evidence'][0]['visible_text'];expected={'task-count':text['count'],'task-first-title':text['titles'][0],'task-first-value':text['values'][0]};paints=[]
            for identity,label in expected.items():
                matches=[v for v in row['paint_entries'] if v['identifier']==identity and v.get('text')==label and finite(v['renderTime']) and v['renderTime']>0 and v['rect']['width']>0 and v['rect']['height']>0]
                require(matches,'content-matched visible paint missing');paints.append(matches[0]['renderTime'])
            require(row['critical_content_paint_ms']==max(paints) and max(paints)<=(done-dispatch)/1e6,'browser paint clock exceeds probe')
            require(len(row['navigation'])==1 and all(finite(v) and v>=0 for v in row['navigation'][0].values()),'native navigation evidence differs')
            qualified=actual-offset<=config['max_dispatch_lag_s'] and done<=end
        else:
            require(row['checks_passed'] is False and row.get('error_type'),'failed probe lacks retained failure')
            if row['error_type']=='BrowserTaskApplicationFailure':
                state=row.get('failure_state',{});kind=state.get('action_error');failed_path='/api/task' if kind=='Task refresh failed' else '/api/task/mutate'
                require(kind in ('Action failed','Task refresh failed') and state.get('action_status')=='Unable to complete action. Please try again.'
                    and any(r['path']==failed_path and 400<=r['status']<=599 and r.get('json',{}).get('ok') is False for r in row['responses']),
                    'task failure lacks visible and HTTP failure evidence')
                if kind=='Task refresh failed':require(state.get('app_outcome')=='failed' and state.get('error_text')=='Unable to load. Please try again.'
                    and state.get('primary_disabled') is True and state.get('secondary_disabled') is True,'failed refresh left active controls or incorrect visible state')
            if row['outcome']=='dispatch_lag_drop':require(actual-offset>config['max_dispatch_lag_s'] and not row['requests'],'false browser drop')
        scored.append({'id':index,'outcome':row['outcome'],'qualified':qualified,'critical_content_paint_ms':row.get('critical_content_paint_ms')})
    return {'probes':scored,'eligible':all(r['qualified'] for r in scored)}


def audit_cell(case,packet,bundles,browser=None):
    """Business/native/receipt audit. Source, gateway, resource and route audits are additional gates."""
    state=packet['state'];task=case['task'];require(state['query_kind']==case['candidate'] and state['seed']['rows']==case['rows'],'fixture identity differs')
    reference=(InventoryScaleReference if task=='inventory' else AnalyticsScaleReference)(state);final=reference.verify_final();raw=packet['backend_events'];events={e['operation_id']:e for e in raw}
    require(len(events)==len(raw),'duplicate native operation identity');allowed=set();writes={};acknowledged={};rows=[]
    for entry in bundles:
        rows=verify_operations(entry['bundle'],entry.get('plan'),entry['prefix'],task,reference,events,entry['cpu'],allowed,writes,acknowledged,serial=entry.get('serial',False),closed=entry.get('closed',False))
    browser_summary=None
    if browser:browser_summary=verify_browser(browser['report'],browser['config'],task,reference,events,allowed,writes,acknowledged)
    name='operation_id' if task=='inventory' else 'event_id';operations={r[name]:r for r in state['operations']}
    require(set(events)<=allowed and set(operations)<=set(writes),'unoffered native work or committed business operation')
    for identity,operation in operations.items():require(operation['request']==writes[identity],'committed business payload differs from offer')
    for identity,result in acknowledged.items():require(identity in operations and operations[identity]['result']==result,'acknowledgement differs from committed ledger')
    for event in raw:
        require(event['task']==task and event['query_kind']==case['candidate'] and event['authorization']==packet['authorization'],'native authorization/task differs')
        if event['backend'] is not None:
            timing=event['backend'];require(all(finite(timing[k]) and timing[k]>=0 for k in ('pool_wait_ms','transaction_wall_ms')) and
                0<int(timing['start_monotonic_ns'])<=int(timing['finish_monotonic_ns'])<=int(event['monotonic_ns']) and
                timing['transaction_wall_ms']==(int(timing['finish_monotonic_ns'])-int(timing['start_monotonic_ns']))/1e6,'native transaction clock differs')
    return {'status':'passed','integrity_verified':True,'case':case,'final_oracle':final,'ambiguous_writes':sorted(set(operations)-set(acknowledged)),
        'browser':browser_summary,'operations':len(rows),'completed':sum(r['outcome']=='completed' for r in rows)}


def derive_profile(task,candidate,rows,source,selected):
    """Two audited serial renderers, three quiet probes each, and no loaded values."""
    require(set(selected)=={'ssr','csr'},'both serial renderers required');values={k:[] for k in ('read','write_a','write_b')};sizes=[];payloads=[];paints={};reasons=set();inputs=[]
    for renderer,value in selected.items():
        case=value['case'];audit=value['audit'];bundle=value['bundle'];packet=value['packet'];report=value['browser_report'];require(case['phase']=='serial' and case['task']==task and case['candidate']==candidate and case['rows']==rows and case['strategy']==renderer and audit['integrity_verified'] is True,'unaudited or nonserial profile input')
        records=bundle['operations'];counts=Counter('read' if r['kind']=='read' else r['mutation'] for r in records);require(counts=={'read':36,'write_a':12,'write_b':12},'fixed serial operation budget differs')
        if any(r['outcome']!='completed' for r in records):reasons.add('logical_completion')
        if audit['ambiguous_writes']:reasons.add('ambiguous_profile_write')
        if not audit['browser']['eligible']:reasons.add('quiet_browser_completion')
        inputs.append({'case':case['name'],'packet_sha256':digest(packet),'bundle_sha256':digest(bundle),'browser_sha256':digest(report),'audit_sha256':digest(audit)})
        if reasons:continue
        events={r['operation_id']:r for r in packet['backend_events']}
        for row in records:
            kind='read' if row['kind']=='read' else row['mutation'];native=events[case['name']+'-main-'+str(row['id'])]['backend']['transaction_wall_ms'];require(finite(native) and native>0,'positive serial native transaction clock required');values[kind].append(native)
            if kind=='read':
                require(len(row['steps'])==(1 if renderer=='ssr' else 2),'serial logical request graph differs');sizes.append(len(row['read_evidence']['items' if task=='inventory' else 'groups']))
                if renderer=='csr':payloads.append(row['steps'][-1]['bytes'])
        paints[renderer]=statistics.median(r['critical_content_paint_ms'] for r in audit['browser']['probes'])
    if reasons:return {'status':'unusable','performance_failure_reasons':sorted(reasons),'evidence':inputs,'profile':None}
    features={'read_service_median_ms':statistics.median(values['read']),'read_service_cv':statistics.stdev(values['read'])/statistics.mean(values['read']),
        'write_a_service_median_ms':statistics.median(values['write_a']),'write_b_service_median_ms':statistics.median(values['write_b']),
        'ssr_serial_paint_median_ms':paints['ssr'],'csr_serial_paint_median_ms':paints['csr'],'read_payload_bytes':statistics.median(payloads),
        'target_logical_cpus':2,'db_logical_cpus':1,'pool_size':4,'result_rows':statistics.median(sizes),'fixture_rows':rows,'ssr_request_count':1,'csr_request_count':2}
    envelope={'application':APPLICATIONS[task],'query_candidate':candidate,'phase':'permitted_serial_profile','source_commit':source,'evidence_sha256':digest(inputs),'features':features};validate_profile(envelope)
    return {'status':'usable','performance_failure_reasons':[],'evidence':inputs,'profile':envelope,'native_samples':{k:len(v) for k,v in values.items()}}
