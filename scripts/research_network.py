"""OIDC-only research nodes with script-enforced own-node cleanup.

The provider's Devices Core Write scope is broader than this script: its tag
selector does not establish a tag boundary for device removal. Use only a
dedicated research tailnet after explicit approval of that scope.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.error import HTTPError
from urllib.parse import urlencode,urlsplit
from urllib.request import Request,urlopen

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from experiments.study_design.decision import require
from experiments.study_design.public_github import GitHub

VERSION='1.104.4'
BINARY_SHA='624ea093d47d62cf56f17b8332d74b8decda4a1faa610ebc60a1de334b2b3dc9'
TAG='tag:qrl-main-ci'


class NetworkAPIError(RuntimeError):
    def __init__(self,status):self.status=status;super().__init__('Research network API status '+str(status))


def request_json(url,*,headers=None,data=None,method=None):
    try:
        with urlopen(Request(url,headers=headers or {},data=data,method=method),timeout=60) as response:
            raw=response.read();return json.loads(raw) if raw else {}
    except HTTPError as error:raise NetworkAPIError(error.code) from None


def access_token():
    endpoint=os.environ['ACTIONS_ID_TOKEN_REQUEST_URL'];parsed=urlsplit(endpoint)
    require(parsed.scheme=='https' and parsed.hostname.endswith('.actions.githubusercontent.com'),'GitHub native workload-token endpoint required')
    endpoint+=('&' if parsed.query else '?')+urlencode({'audience':os.environ['QRL_TS_AUDIENCE']})
    token=request_json(endpoint,headers={'Authorization':'Bearer '+os.environ['ACTIONS_ID_TOKEN_REQUEST_TOKEN']})['value']
    return request_json('https://api.tailscale.com/api/v2/oauth/token-exchange',data=urlencode({'client_id':os.environ['QRL_TS_CLIENT_ID'],'jwt':token}).encode(),
        headers={'Content-Type':'application/x-www-form-urlencoded'})['access_token']


def api(path,*,method='GET',token):
    require(path.startswith(('device/','tailnet/-/devices')) and '..' not in path,'restricted device API path required')
    return request_json('https://api.tailscale.com/api/v2/'+path,method=method,headers={'Authorization':'Bearer '+token})


def hostname(task,role):
    require(task in ('inventory','analytics') and role in ('target','observer') and os.environ['GITHUB_RUN_ATTEMPT']=='1','fixed research role/first attempt required')
    return 'qrl-'+os.environ['GITHUB_RUN_ID']+('0' if task=='inventory' else '1')+'-1-'+role


def verify_own(device,node_id,name):
    require(device['nodeId']==node_id and device['hostname']==name and device['tags']==[TAG] and device['isEphemeral'] is False and device.get('isExternal') is not True,
        'device is not this run\'s exclusively tagged standard research node')
    return True


def install_client():
    url=f'https://pkgs.tailscale.com/stable/tailscale_{VERSION}_amd64.tgz'
    with urlopen(url,timeout=60) as response:raw=response.read()
    require(hashlib.sha256(raw).hexdigest()==BINARY_SHA,'official pinned client tarball checksum differs')
    with tempfile.TemporaryDirectory(prefix='qrl-tailscale-install-') as temp,tarfile.open(fileobj=io.BytesIO(raw),mode='r:gz') as archive:
        for name in ('tailscale','tailscaled'):
            member=archive.getmember(f'tailscale_{VERSION}_amd64/{name}');require(member.isfile(),'official client binary missing')
            path=Path(temp)/name;path.write_bytes(archive.extractfile(member).read());subprocess.run(['sudo','-n','install','-m','0755',str(path),'/usr/local/bin/'+name],check=True,capture_output=True)


def register(output,name):
    token=access_token();listed=api('tailnet/-/devices',token=token)['devices']
    allowed={f"qrl-{os.environ['GITHUB_RUN_ID']}{suffix}-1-{role}" for suffix in ('0','1') for role in ('target','observer')}-{name}
    require(len(listed)<4 and len({d.get('hostname') for d in listed})==len(listed)
        and all(d.get('hostname') in allowed and d.get('tags')==[TAG] and d.get('isEphemeral') is False and d.get('isExternal') is not True for d in listed),
        'dedicated research network must contain only this run\'s other standard nodes; unrelated or previous nodes require attention')
    install_client();subprocess.run(['sudo','-n','install','-d','-m','0755','/var/run/tailscale'],check=True,capture_output=True)
    with Path('/tmp/qrl-tailscaled-private.log').open('w') as log:
        # Private daemon/auth output is never a public research artifact. Filtered
        # role/route records are retained by the measurement collector instead.
        subprocess.Popen(['sudo','-n','/usr/local/bin/tailscaled','--state=mem:','--socket=/var/run/tailscale/tailscaled.sock','--port=41641'],stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT)
    deadline=time.monotonic()+10
    while not Path('/var/run/tailscale/tailscaled.sock').exists():
        require(time.monotonic()<deadline,'research overlay socket did not become ready');time.sleep(.1)
    client_id=os.environ['QRL_TS_CLIENT_ID']+'?ephemeral=false&preauthorized=true'
    command=['sudo','-n','-E','/usr/local/bin/tailscale','up','--client-id='+client_id,'--audience='+os.environ['QRL_TS_AUDIENCE'],'--advertise-tags='+TAG,
        '--hostname='+name,'--accept-routes=false','--accept-dns=false','--timeout=2m']
    result=subprocess.run(command,capture_output=True,timeout=150);require(result.returncode==0,'standard tagged research-node registration failed; no auth output exported')
    status=json.loads(subprocess.run(['tailscale','status','--json'],check=True,capture_output=True,text=True).stdout);node_id=status['Self']['ID']
    record={'hostname':name,'node_id':node_id,'tag':TAG,'registration_mode':'standard-tagged-v1','client_version':VERSION,'client_tarball_sha256':BINARY_SHA,'verified_standard_node':False}
    output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps(record,indent=2)+'\n')
    device=api('device/'+node_id,token=token);verify_own(device,node_id,name);require(status['BackendState']=='Running' and status['TUN'] is True,'running kernel overlay required')
    record['verified_standard_node']=True;output.write_text(json.dumps(record,indent=2)+'\n');print(json.dumps({'status':'registered','hostname':name,'tag':TAG,'ephemeral':False}))


def cleanup(output,name):
    if not output.exists():
        try:
            status=json.loads(subprocess.run(['tailscale','status','--json'],check=True,capture_output=True,text=True).stdout);node_id=status['Self']['ID']
            require(status['Self']['HostName']==name,'unrecorded overlay is not this run\'s research node')
        except (FileNotFoundError,subprocess.CalledProcessError,KeyError):
            print(json.dumps({'status':'no registered local research node'}));return
    else:
        record=json.loads(output.read_bytes());require(record['hostname']==name and record['tag']==TAG,'cleanup ownership record differs');node_id=record['node_id']
    require(re.fullmatch('[A-Za-z0-9]+',node_id),'bounded native node identity required');token=access_token()
    try:device=api('device/'+node_id,token=token)
    except NetworkAPIError as error:
        if error.status!=404:raise
        device=None
    if device is not None:verify_own(device,node_id,name);api('device/'+node_id,method='DELETE',token=token)
    try:api('device/'+node_id,token=token)
    except NetworkAPIError as error:require(error.status==404,'node deletion verification failed')
    else:raise ValueError('standard research node remains registered')
    subprocess.run(['sudo','-n','tailscale','logout'],capture_output=True,check=False,timeout=30)
    path=output.with_name('network-cleanup.json');path.write_text(json.dumps({'status':'removed','hostname':name,'node_id':node_id,'tag':TAG,'verified_absent':True},indent=2)+'\n');print(json.dumps({'status':'removed','hostname':name,'tag':TAG}))


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=('register','cleanup'));parser.add_argument('--task',required=True);parser.add_argument('--role',required=True);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    require(os.environ.get('GITHUB_ACTIONS')=='true' and os.environ.get('RUNNER_OS')=='Linux' and os.environ.get('RUNNER_ARCH')=='X64' and os.environ.get('QRL_STANDARD_TAGGED_NETWORK_APPROVED')=='true','explicitly approved disposable Linux x64 research-network setup required')
    name=hostname(args.task,args.role)
    if args.action=='register':
        github=GitHub(os.environ['GITHUB_REPOSITORY'],os.environ.pop('QRL_GITHUB_READ_TOKEN'));github.require_public();require(github.repository=='shravaniparsi/query-rendering-benchmark','unexpected research repository');register(args.output,name)
    else:cleanup(args.output,name)


if __name__=='__main__':main()
