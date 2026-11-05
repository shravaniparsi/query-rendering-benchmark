"""Preserve application-proxy delivery failures separately from control integrity."""
import json
import time
from urllib.error import HTTPError,URLError

POLICY='validated-application-proxy-transport-v1.1'


def forward(transport,request,send,target,case_name,path,method,operation_id):
    """One upstream attempt; expected transport errors remain failed HTTP delivery."""
    def record(leg,error,status):
        event={'case':case_name,'path':path,'method':method,'operation_id':operation_id,
               'leg':leg,'status':status,'error_type':type(error).__name__,
               'reason_type':type(getattr(error,'reason',error)).__name__,
               'errno':getattr(getattr(error,'reason',error),'errno',None),
               'monotonic_s':time.monotonic()}
        with target.lock:
            event['sequence']=len(target.proxy_transport_failures)
            target.proxy_transport_failures.append(event)
    try:
        try:
            with transport.open(request,timeout=15) as response:
                status,data,kind=response.status,response.read(),response.headers.get('Content-Type','application/octet-stream')
        except HTTPError as error:
            status,data,kind=error.code,error.read(),error.headers.get('Content-Type','application/json')
    except (URLError,OSError) as error:
        status=502;record('upstream',error,status)
        data=json.dumps({'error_type':type(error).__name__,'failure_code':'proxy_upstream_transport_failure'}).encode()
        kind='application/json'
    try:send(status,data,kind)
    except OSError as error:record('downstream',error,status)
