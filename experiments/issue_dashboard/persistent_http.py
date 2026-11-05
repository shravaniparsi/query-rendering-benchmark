"""Bounded, observed HTTP/1.1 ownership for correctness-stage connection controls."""
import hashlib,http.client,ipaddress,os,socket,threading,time,uuid
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import urlsplit

SERVER_IDLE_S=5
CLIENT_IDLE_S=2
MAX_REQUESTS=32
CONNECTION_LIMIT=32


def cpus():return sorted(os.sched_getaffinity(0)) if hasattr(os,'sched_getaffinity') else None


class PersistentHandler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    disable_nagle_algorithm=True
    def setup(self):
        super().setup();self.connection_id=uuid.uuid4().hex;self.response_index=0;self.connection.settimeout(SERVER_IDLE_S)
    def handle_one_request(self):
        try:return super().handle_one_request()
        finally:self.connection.settimeout(SERVER_IDLE_S)


class PersistentServer(ThreadingHTTPServer):
    def __init__(self,address,handler,connection_limit=CONNECTION_LIMIT):
        if type(connection_limit) is not int or not 1<=connection_limit<=128:raise ValueError('bounded connection limit required')
        super().__init__(address,handler);self.connection_slots=threading.BoundedSemaphore(connection_limit);self.connection_rejections=0
    def process_request(self,request,client_address):
        if not self.connection_slots.acquire(blocking=False):
            self.connection_rejections+=1;self.shutdown_request(request);return
        try:super().process_request(request,client_address)
        except BaseException:self.connection_slots.release();raise
    def process_request_thread(self,request,client_address):
        try:super().process_request_thread(request,client_address)
        finally:self.connection_slots.release()


def observe_send(handler,target,status,body,send):
    handler.response_index+=1
    event={'connection_id':handler.connection_id,'response_index':handler.response_index,
        'path':urlsplit(handler.path).path[:2048],'method':handler.command,
        'operation_id':handler.headers.get('X-QRL-Operation','')[:128],
        'client_address':list(handler.client_address),'server_address':list(handler.server.server_address),
        'http_version':handler.protocol_version,'status':status,'body_bytes':len(body),
        'body_sha256':hashlib.sha256(body).hexdigest(),'allowed_cpus':cpus(),
        'tcp_nodelay_raw':handler.connection.getsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY),
        'outcome':'sent','error_type':None}
    try:send()
    except OSError as error:event.update(outcome='failed',error_type=type(error).__name__);raise
    finally:
        event['close_after_response']=handler.close_connection
        with target.lock:target.connection_observations.append(event)


class OwnedSession:
    """Exactly one HTTP connection owned by one calling thread; never retry a request."""
    def __init__(self,host,port,*,persistent,max_requests=MAX_REQUESTS,idle_s=CLIENT_IDLE_S,timeout_s=5,expected_cpus=None):
        address=ipaddress.ip_address(host)
        if address.version!=4 or not(address.is_loopback or address in ipaddress.ip_network('100.64.0.0/10')):raise ValueError('literal research/loopback IPv4 required')
        if type(port) is not int or not 1<=port<=65535 or type(persistent) is not bool:raise ValueError('invalid endpoint/mode')
        if type(max_requests) is not int or not 1<=max_requests<=128 or not 0<idle_s<SERVER_IDLE_S or not 0<timeout_s<=15:raise ValueError('invalid connection budget')
        self.host,self.port,self.persistent=host,port,persistent;self.max_requests,self.idle_s,self.timeout_s=max_requests,idle_s,timeout_s
        self.owner=threading.current_thread();self.expected_cpus=expected_cpus;self.connection=None;self.used=0;self.last=None;self.records=[]
    def check_owner(self):
        if threading.current_thread() is not self.owner:raise RuntimeError('HTTP session used by a different thread')
        if self.expected_cpus is not None and cpus()!=self.expected_cpus:raise RuntimeError('HTTP owner CPU mask differs')
    def close(self):
        self.check_owner()
        if self.connection:self.connection.close();self.connection=None
        self.used=0;self.last=None
    def request(self,method,path,*,operation_id,body=None):
        self.check_owner();parsed=urlsplit(path)
        if method not in ('GET','POST') or parsed.scheme or parsed.netloc or not path.startswith('/') or len(path)>2048 or parsed.fragment:raise ValueError('bounded relative GET/POST required')
        if not isinstance(operation_id,str) or not 0<len(operation_id)<=128:raise ValueError('operation identity required')
        if body is not None and (not isinstance(body,bytes) or len(body)>32768):raise ValueError('bounded byte body required')
        if method=='GET' and body is not None:raise ValueError('GET body is not part of the workload')
        if self.connection and (not self.persistent or self.used>=self.max_requests or time.monotonic()-self.last>=self.idle_s):self.close()
        if self.connection is None:self.connection=http.client.HTTPConnection(self.host,self.port,timeout=self.timeout_s)
        connection=self.connection
        row={'operation_id':operation_id,'method':method,'path':path,'persistent':self.persistent,'outcome':'failed','error_type':None,
            'allowed_cpus':cpus(),'status':None,'server_connection_id':None,'body_bytes':None,'body_sha256':None,
            'client_address':None,'peer_address':None,'http_version':None,'will_close':None,'attempts':1}
        try:
            headers={'Cache-Control':'no-store','X-QRL-Operation':operation_id,'Connection':'keep-alive' if self.persistent else 'close'}
            if body is not None:headers.update({'Content-Type':'application/json','X-QRL-Control':'engineering'})
            connection.request(method,path,body=body,headers=headers)
            row.update(client_address=list(connection.sock.getsockname()),peer_address=list(connection.sock.getpeername()))
            response=connection.getresponse();raw=response.read(4*1024*1024+1)
            if len(raw)>4*1024*1024:raise ValueError('response exceeds correctness-stage bound')
            row.update(status=response.status,server_connection_id=response.getheader('X-QRL-Connection'),http_version=response.version,
                will_close=response.will_close,body_bytes=len(raw),body_sha256=hashlib.sha256(raw).hexdigest(),outcome='completed')
            if response.version!=11 or not row['server_connection_id']:raise ValueError('observed HTTP/1.1 gateway identity required')
            self.used+=1;self.last=time.monotonic()
            if response.will_close or not self.persistent:self.close()
            return row,raw
        except (OSError,http.client.HTTPException) as error:
            row.update(outcome='failed',error_type=type(error).__name__);self.close();return row,None
        except Exception as error:
            row.update(outcome='invalid',error_type=type(error).__name__);self.close();raise
        finally:self.records.append(row)
