"""Small GitHub REST client for immutable calendar claims and release assets."""
import hashlib
import json
from pathlib import Path
import re
from urllib.error import HTTPError
from urllib.parse import quote,urlsplit
from urllib.request import Request,urlopen

from .decision import require


class APIError(RuntimeError):
    def __init__(self,status):self.status=status;super().__init__('GitHub API status '+str(status))


class GitHub:
    def __init__(self,repository,token):
        require(isinstance(repository,str) and re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repository) and isinstance(token,str) and token,'repository and ephemeral token required')
        self.repository=repository;self.token=token;self.base='https://api.github.com/repos/'+repository
    def call(self,method,path,*,value=None,raw=None,url=None):
        endpoint=self.base+path if url is None else url;parsed=urlsplit(endpoint)
        base_path='/repos/'+self.repository
        require(parsed.scheme=='https' and parsed.hostname in ('api.github.com','uploads.github.com') and (parsed.path==base_path or parsed.path.startswith(base_path+'/')),'confined GitHub API endpoint required')
        require(value is None or raw is None,'one request body required');body=json.dumps(value).encode() if value is not None else raw
        request=Request(endpoint,method=method,data=body,headers={'Authorization':'Bearer '+self.token,'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28',
            'Content-Type':'application/json' if raw is None else 'application/octet-stream','User-Agent':'qrl-research-evidence'})
        try:
            with urlopen(request,timeout=60) as response:return json.load(response)
        except HTTPError as error:raise APIError(error.code) from None
    def optional(self,path):
        try:return self.call('GET',path)
        except APIError as error:
            if error.status==404:return None
            raise
    def require_public(self):
        result=self.call('GET','');require(result['private'] is False and result['full_name']==self.repository,'free acquisition requires the actual public repository');return result
    def ensure_release(self,tag,source,release_sha):
        require(re.fullmatch('[a-z0-9-]{1,100}',tag) and re.fullmatch('[0-9a-f]{40}',source) and re.fullmatch('[0-9a-f]{64}',release_sha),'valid immutable release identity required')
        path='/releases/tags/'+tag;result=self.optional(path)
        if result is None:
            try:result=self.call('POST','/releases',value={'tag_name':tag,'target_commitish':source,'name':tag,'body':'Frozen synthetic research evidence. Release SHA-256: '+release_sha,'draft':False,'prerelease':False})
            except APIError as error:
                if error.status!=422:raise
                result=self.call('GET',path)
        require(result['tag_name']==tag and result['target_commitish']==source and release_sha in result['body'],'existing release belongs to a different frozen source/envelope');return result
    def upload(self,release,path):
        path=Path(path);name=path.name;raw=path.read_bytes();expected='sha256:'+hashlib.sha256(raw).hexdigest();require(0<len(raw)<2*1024**3,'bounded release asset required')
        for page in range(1,12):
            records=self.call('GET',f'/releases/{release["id"]}/assets?per_page=100&page={page}')
            matches=[r for r in records if r['name']==name]
            if matches:
                require(len(matches)==1 and matches[0]['size']==len(raw) and matches[0]['digest']==expected,'existing immutable asset has different bytes');return matches[0]
            if len(records)<100:break
        endpoint=release['upload_url'].split('{')[0]+'?name='+quote(name,safe='');result=self.call('POST','',raw=raw,url=endpoint)
        require(result['name']==name and result['size']==len(raw) and result['digest']==expected,'uploaded release asset digest differs');return result


def verify_claim(api,claim_sha,*,day,mode,run_id,source,release_sha):
    require(re.fullmatch('[0-9a-f]{40}',claim_sha),'annotated date-claim object required');tag=api.call('GET','/git/tags/'+claim_sha);message=json.loads(tag['message'])
    require(tag['object']=={'type':'commit','sha':source,'url':api.base+'/git/commits/'+source} and message=={'collection_date_utc':day,'mode':mode,'run_id':run_id,'run_attempt':'1','source_commit':source,'release_sha256':release_sha},'date claim does not bind this exact attempt/source/envelope')
    reference=api.call('GET','/git/ref/tags/'+tag['tag']);require(reference['object']['sha']==claim_sha and reference['object']['type']=='tag','date claim is not the current immutable calendar reference');return message
