"""Opt-in application-free responses through the unchanged research gateway send path."""
import hashlib,json,os,socket,time
from pathlib import Path
from urllib.request import Request
from .arrivals import private_transport
from .offhost_engineering import require,save

POLICY='matched-gateway-path-v1'
BODY_BYTES=1024


def protocol(smoke):
    return {'version':POLICY,'body_bytes':BODY_BYTES,'probes_per_window':3 if smoke else 16,
        'windows':['pre','post'],'timeout_s':5,'retries':0,
        'transport':'cached urllib handler; fresh TCP connection; HTTP/1.0 gateway response',
        'server_send':'existing gateway send; headers then body; observed TCP_NODELAY',
        'feature_scope':'serial-phase controls only; loaded-case controls are diagnostics'}


def identity(run,case,window,index):
    return {'protocol':POLICY,'run_id':str(run),'case':case,'window':window,'index':index}


def body_for(value):
    raw=json.dumps(value,sort_keys=True,separators=(',',':')).encode()
    require(len(raw)<BODY_BYTES,'path identity exceeds body budget')
    return raw+b' '*(BODY_BYTES-len(raw))


def serve(handler,target,path):
    """Validate before responding; ordinary delivery failures stay explicit native events."""
    parts=path.split('/');require(len(parts)==7 and parts[1:3]==['qrl','path-probe'],'invalid path control route')
    _,_,_,case,window,index_text,run=parts
    index=int(index_text);require(str(index)==index_text and window in ('pre','post'),'invalid path control identity')
    with target.lock:
        require(target.runtime is not None and case==target.case['name'] and run==target.own['run_id'],'no matching path control case/run')
        require(target.budget is None,'path controls forbidden during measurement')
        require((window=='pre')==(target.wal_before is None),'path control window differs from measurement state')
        if window=='post':require((target.output/case/'wal-after.json').is_file(),'post control before measurement close')
        events=target.path_control_events.setdefault(case,[]);count=protocol(target.smoke)['probes_per_window']
        key=index+(0 if window=='pre' else count)
        prior=[e['index']+(0 if e['window']=='pre' else count) for e in events]
        require(0<=index<count and (not prior or key>prior[-1]),'duplicate/out-of-order path control')
        value=identity(run,case,window,index);raw=body_for(value)
        require(handler.headers.get('X-QRL-Operation')==f'{case}-path-{window}-{index}','path operation differs')
        event={**value,'body_bytes':len(raw),'body_sha256':hashlib.sha256(raw).hexdigest(),
            'start_ns':str(time.perf_counter_ns()),'allowed_cpus':sorted(os.sched_getaffinity(0)),
            'server_address':list(handler.server.server_address),'client_address':list(handler.client_address),
            'tcp_nodelay_raw':handler.connection.getsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY),
            'http_version':handler.protocol_version,'send_calls':1,'outcome':'sent','error_type':None}
        event['tcp_nodelay_enabled']=event['tcp_nodelay_raw']!=0
        try:handler.send(200,raw)
        except OSError as error:event.update(outcome='failed',error_type=type(error).__name__)
        finally:
            event['finish_ns']=str(time.perf_counter_ns());events.append(event);target.last=time.monotonic()
            with (target.output/case/'path-controls.jsonl').open('a') as stream:stream.write(json.dumps(event,sort_keys=True)+'\n')


def collect(base,directory,case,window,own,smoke):
    spec=protocol(smoke);rows=[];start=time.perf_counter_ns()
    filename=directory/('path-'+window+'.json')
    for index in range(spec['probes_per_window']):
        value=identity(own['run_id'],case['name'],window,index);expected=body_for(value)
        row={**value,'start_ns':str(time.perf_counter_ns()),'allowed_cpus':sorted(os.sched_getaffinity(0)),
            'outcome':'failed','error_type':None,'headers_ns':None,'body_ns':None,'validated_ns':None,
            'status':None,'http_version':None,'will_close':None,'body_bytes':None,'body_sha256':None}
        request=Request(base+f'/qrl/path-probe/{case["name"]}/{window}/{index}/{own["run_id"]}',
            headers={'Cache-Control':'no-store','X-QRL-Operation':f'{case["name"]}-path-{window}-{index}'})
        row['url']=request.full_url
        try:
            with private_transport()(request,timeout=spec['timeout_s']) as response:
                row.update(headers_ns=str(time.perf_counter_ns()),status=response.status,http_version=response.version,will_close=response.will_close)
                raw=response.read(BODY_BYTES+1);row.update(body_ns=str(time.perf_counter_ns()),body_bytes=len(raw),body_sha256=hashlib.sha256(raw).hexdigest())
                require(response.status==200 and response.version==10 and response.will_close and raw==expected,'path response differs')
                row.update(validated_ns=str(time.perf_counter_ns()),outcome='completed')
        except OSError as error:row['error_type']=type(error).__name__
        row['finish_ns']=str(time.perf_counter_ns());rows.append(row)
        save(filename,{'protocol':spec,'case':case['name'],'window':window,'start_ns':str(start),
            'finish_ns':row['finish_ns'],'records':rows})
    return rows
