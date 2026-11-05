"""Bounded research HTTP/1.1 gateway with guarded same-origin task mutations."""
import json
from urllib.parse import urlsplit,parse_qs
from urllib.request import Request
from .inventory import require
from experiments.issue_dashboard.offhost_engineering import TRANSPORT
from experiments.issue_dashboard.persistent_http import PersistentHandler,PersistentServer,observe_send
from experiments.issue_dashboard.proxy_transport import forward
from scripts.offhost_connectivity import check_pair


def gateway(target,bind_ip,peer_ip,port=8765):
    class Handler(PersistentHandler):
        def log_message(self,*args):pass
        def send(self,status,body,content_type='application/json'):
            try:return observe_send(self,target,status,body,lambda:self.send_body(status,body,content_type))
            finally:
                with target.lock:target.identity_observations.append({'connection_id':self.connection_id,'response_index':self.response_index,
                    'path':urlsplit(self.path).path,'operation_id':self.headers.get('X-QRL-Operation',''),'source':getattr(self,'identity_source','header_or_empty')})
        def send_body(self,status,body,content_type):
            self.send_response(status);self.send_header('Content-Type',content_type);self.send_header('X-QRL-Connection',self.connection_id)
            if self.close_connection:self.send_header('Connection','close')
            self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(body)
        def handle_request(self):
            self.identity_source='header_or_empty';parsed=urlsplit(self.path);path=parsed.path
            if self.client_address[0]!=peer_ip:self.send(403,b'{}');return
            try:
                require(not parsed.scheme and not parsed.netloc and not parsed.fragment and len(self.path)<=2048,'relative bounded path required')
                public_origin=f'http://{bind_ip}:{self.server.server_port}';browser_write=self.command=='POST' and path=='/api/task/mutate'
                if browser_write:require(self.headers.get('Origin')==public_origin,'same-origin task mutation required')
                else:require(not self.headers.get('Origin'),'browser-origin control rejected')
                body=None
                if self.command=='POST':
                    require(self.headers.get('Content-Type','').split(';')[0]=='application/json','JSON request required')
                    size=int(self.headers.get('Content-Length','0'));require(0<size<=32768,'bounded body required')
                    self.connection.settimeout(15);body=self.rfile.read(size);require(len(body)==size,'incomplete body')
                if self.command=='GET' and path in ('/task/ssr','/task/csr') and not self.headers.get('X-QRL-Operation'):
                    values=parse_qs(parsed.query).get('operation_id',[])
                    require(len(values)==1 and 0<len(values[0])<=80,'bounded document query identity required')
                    self.headers['X-QRL-Operation']=values[0];self.identity_source='document_query_parameter'
                if path=='/manifest' and self.command=='GET':value=target.own
                elif path=='/qrl/hello' and self.command=='POST':
                    value=json.loads(body);check_pair(target.own,value);require(target.remote in (None,value),'different observer');target.remote=value;value={'accepted':True}
                elif path=='/qrl/finish' and self.command=='POST':
                    require(json.loads(body)=={'observer':target.remote} and target.index==len(target.cases) and target.runtime is None,'incomplete acquisition')
                    self.send(200,b'{"finished":true}');target.finished=True;return
                elif path.startswith('/qrl/path-probe/') and self.command=='GET':
                    from experiments.issue_dashboard.path_controls import serve
                    serve(self,target,path);return
                elif path.startswith('/qrl/') and self.command=='POST':value=target.control(path[len('/qrl/'):],json.loads(body))
                else:
                    with target.lock:
                        require(target.runtime is not None,'no active task')
                        if self.command=='GET' and path in ('/health','/task/ssr','/task/csr','/api/task','/static/client.js'):url=target.runtime['url']+self.path
                        elif browser_write:url=target.runtime['url']+path
                        elif self.command=='POST' and path=='/control/mutate':
                            require(self.headers.get('X-QRL-Control')=='engineering','private engineering control required');url=target.runtime['origin'].url+path
                        else:self.send(404,b'{}');return
                        case=target.case['name']
                    headers={'Cache-Control':'no-store','X-QRL-Operation':self.headers.get('X-QRL-Operation','')[:128]}
                    if body is not None:headers['Content-Type']='application/json'
                    if browser_write:headers['Origin']=public_origin
                    elif body is not None:headers['X-QRL-Control']='engineering'
                    forward(TRANSPORT,Request(url,data=body,headers=headers),self.send,target,case,path,self.command,headers['X-QRL-Operation']);return
                self.send(200,json.dumps(value).encode())
            except Exception as error:
                with target.lock:target.errors.append({'path':path,'error_type':type(error).__name__})
                self.send(400,json.dumps({'error_type':type(error).__name__}).encode())
        do_GET=handle_request;do_POST=handle_request
    target.errors=[];target.connection_observations=[];target.identity_observations=[];target.proxy_transport_failures=[];target.path_control_events={}
    server=PersistentServer((bind_ip,port),Handler);server.daemon_threads=True;server.timeout=1;return server
