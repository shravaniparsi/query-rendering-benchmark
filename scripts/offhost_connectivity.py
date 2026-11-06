"""Bounded two-runner private-network preflight. No database or benchmark service."""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import time
from urllib.request import Request,build_opener,ProxyHandler

NETWORK=ipaddress.ip_network('100.64.0.0/10')
PORT=8765


def node_name(run,attempt,role):
    if not run.isdigit() or not attempt.isdigit() or role not in ('target','observer'):raise ValueError('invalid run identity')
    return f'qrl-{run}-{attempt}-{role}'


def private_ip(values):
    matches=[str(ipaddress.ip_address(v)) for v in values if ipaddress.ip_address(v).version==4 and ipaddress.ip_address(v) in NETWORK]
    if len(matches)!=1:raise ValueError('one private overlay IPv4 required')
    return matches[0]


def route(peer):
    if peer.get('PeerRelay'):return 'peer-relay'
    if peer.get('CurAddr'):return 'direct'
    if peer.get('Relay') and peer.get('Active'):return 'relay'
    return 'unknown'


def select_nodes(status,self_name,peer_name):
    if status.get('BackendState')!='Running' or status.get('TUN') is not True:raise ValueError('running kernel overlay required')
    own=status['Self']
    if own.get('HostName')!=self_name:raise ValueError('unexpected local overlay name')
    # The pinned client's JSON encodes an empty peer map as null during startup.
    # Treat it as no discovered peer so network_state's bounded retry applies.
    peers=[v for v in (status.get('Peer') or {}).values() if v.get('HostName')==peer_name and v.get('Online')]
    if len(peers)!=1:raise ValueError('expected live peer not uniquely available')
    peer=peers[0];private_ip(own['TailscaleIPs']);private_ip(peer['TailscaleIPs'])
    keys=('HostName','TailscaleIPs','Online','Active','CurAddr','Relay','PeerRelay')
    return {'version':status['Version'],'self':{k:own.get(k) for k in keys},
            'peer':{k:peer.get(k) for k in keys},'route':route(peer)}


def network_state(self_name,peer_name,timeout=180,*,require_direct=False,route_history=None):
    deadline=time.monotonic()+timeout
    while True:
        value=json.loads(subprocess.run(['tailscale','status','--json'],check=True,capture_output=True,text=True,timeout=10).stdout)
        try:
            selected=select_nodes(value,self_name,peer_name)
            if route_history is not None:route_history.append(selected['route'])
            if require_direct and selected['route']!='direct':raise ValueError('direct route required before probes')
            return selected
        except ValueError:
            if time.monotonic()>deadline:raise
            time.sleep(1)


def manifest(role,run,attempt,commit):
    if not re.fullmatch('[0-9a-f]{40}',commit):raise ValueError('full source commit required')
    return {'role':role,'run_id':run,'run_attempt':attempt,'source_commit':commit,
        'overlay_name':node_name(run,attempt,role),'hostname':platform.node(),
        'kernel_boot_sha256':hashlib.sha256(Path('/proc/sys/kernel/random/boot_id').read_bytes()).hexdigest(),
        'allowed_cpus':sorted(os.sched_getaffinity(0))}


def check_pair(local,remote):
    if any(local[k]!=remote[k] for k in ('run_id','run_attempt','source_commit')):raise ValueError('cross-run/source peer')
    expected='target' if local['role']=='observer' else 'observer'
    if remote['role']!=expected or remote['overlay_name']!=node_name(local['run_id'],local['run_attempt'],expected):
        raise ValueError('wrong peer role/name')
    if not remote['kernel_boot_sha256'] or local['kernel_boot_sha256']==remote['kernel_boot_sha256']:
        raise ValueError('distinct kernel boots required; containers alone are insufficient')


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--role',choices=('target','observer'),required=True)
    parser.add_argument('--task',choices=('inventory','analytics'),help='separate pair namespace for the four-runner network-only check')
    parser.add_argument('--require-direct',choices=('true','false'),default='true');args=parser.parse_args()
    run,attempt,commit=(os.environ[k] for k in ('GITHUB_RUN_ID','GITHUB_RUN_ATTEMPT','GITHUB_SHA'))
    github_run=run
    if args.task:
        run+=('0' if args.task=='inventory' else '1')
        for key in ('ACTIONS_ID_TOKEN_REQUEST_URL','ACTIONS_ID_TOKEN_REQUEST_TOKEN','GH_TOKEN','GITHUB_TOKEN'):
            os.environ.pop(key,None)
    own=manifest(args.role,run,attempt,commit);peer_name=node_name(run,attempt,'observer' if args.role=='target' else 'target')
    if args.task:own.update(github_run_id=github_run,task=args.task)
    output=Path('ci-artifacts/offhost-connectivity');output.mkdir(parents=True,exist_ok=False)
    report={'role':'two-runner private-network engineering preflight','benchmark_measurements':False,'local':own,'status':'started'}
    try:
        before=network_state(own['overlay_name'],peer_name);report['network_before']=before
        if args.role=='target':
            finish=[];received=set();peer_ip=private_ip(before['peer']['TailscaleIPs'])
            class Handler(BaseHTTPRequestHandler):
                def log_message(self,*args):pass
                def send(self,status,value):
                    body=json.dumps(value).encode();self.send_response(status)
                    self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)))
                    self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(body)
                def do_GET(self):
                    if self.client_address[0]!=peer_ip:self.send(403,{'error':'expected peer required'});return
                    if self.path=='/manifest':self.send(200,own);return
                    match=re.fullmatch('/probe/([0-9])',self.path)
                    if not match:self.send(404,{'error':'not found'});return
                    index=int(match[1]);received.add(index);self.send(200,{'index':index,'run_id':run,'payload':'qrl-connectivity-only'})
                def do_POST(self):
                    try:
                        if self.client_address[0]!=peer_ip or self.path!='/finish':raise ValueError('peer/route')
                        size=int(self.headers.get('Content-Length','0'))
                        if not 0<size<=8192:raise ValueError('body bound')
                        value=json.loads(self.rfile.read(size));check_pair(own,value['observer'])
                        if received!=set(range(10)):raise ValueError('incomplete probes')
                        value['target_network_after']=network_state(own['overlay_name'],peer_name,timeout=5)
                        self.send(200,{'completed':True});finish.append(value)
                    except (ValueError,KeyError):self.send(400,{'error':'invalid completion'})
            server=ThreadingHTTPServer((private_ip(before['self']['TailscaleIPs']),PORT),Handler)
            server.timeout=1;server.daemon_threads=True;deadline=time.monotonic()+240
            try:
                while not finish and time.monotonic()<deadline:server.handle_request()
                if not finish:raise RuntimeError('observer did not complete before deadline')
            finally:server.server_close()
            report.update(remote=finish[0]['observer'],probe_ids=sorted(received));after=finish[0]['target_network_after']
        else:
            address=private_ip(before['peer']['TailscaleIPs']);url=f'http://{address}:{PORT}'
            opener=build_opener(ProxyHandler({}))
            subprocess.run(['tailscale','ping','--c','3','--timeout','5s',address],capture_output=True,text=True,timeout=25,check=False)
            deadline=time.monotonic()+180
            while True:
                try:
                    with opener.open(url+'/manifest',timeout=3) as response:remote=json.load(response)
                    break
                except OSError:
                    if time.monotonic()>deadline:raise
                    time.sleep(1)
            check_pair(own,remote);report['remote']=remote
            # Allow a bounded negotiation interval after actual HTTP handshake.
            # Preserve startup routes; only begin probes once the requested path is ready.
            report['network_after_handshake']=network_state(own['overlay_name'],peer_name)
            report['direct_readiness_routes']=[]
            ready_started=time.monotonic()
            report['network_before_probes']=network_state(own['overlay_name'],peer_name,timeout=30,
                require_direct=args.require_direct=='true',route_history=report['direct_readiness_routes'])
            report['direct_readiness_wait_ms']=(time.monotonic()-ready_started)*1000
            probes=[]
            for index in range(10):
                started=time.perf_counter()
                with opener.open(url+f'/probe/{index}',timeout=5) as response:value=json.load(response)
                elapsed=(time.perf_counter()-started)*1000
                if value!={'index':index,'run_id':run,'payload':'qrl-connectivity-only'}:raise ValueError('probe response differs')
                probes.append({'index':index,'wall_ms':elapsed});time.sleep(.2)
            report['probes']=probes
            after=network_state(own['overlay_name'],peer_name,timeout=10)
            body=json.dumps({'observer':own}).encode()
            with opener.open(Request(url+'/finish',data=body,headers={'Content-Type':'application/json'}),timeout=15) as response:
                if json.load(response)!={'completed':True}:raise ValueError('target did not confirm probes')
        report['network_after']=after
        # Discovery may begin before a direct handshake, so qualify after traffic, not from initial idle state.
        states=[after]+([report['network_before_probes']] if args.role=='observer' else [])
        direct=all(v['route']=='direct' for v in states);report['direct_path_observed']=direct
        if args.require_direct=='true' and not direct:raise RuntimeError('direct overlay path not established')
        report['status']='passed'
        report['scope']='Engineering connectivity only; a future benchmark must validate path stability throughout every window.'
    except Exception as error:report.update(status='failed',error_type=type(error).__name__);raise
    finally:(output/'report.json').write_text(json.dumps(report,indent=2)+'\n')


if __name__=='__main__':main()
