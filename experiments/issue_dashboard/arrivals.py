"""Issue-specific open-loop operations; shared scheduling implementation only."""
from html.parser import HTMLParser
from functools import lru_cache
import json
import math
import random
import time
from urllib.error import HTTPError
from urllib.parse import urlencode,urlparse
from urllib.request import Request,urlopen
from urllib.request import build_opener,ProxyHandler
import ipaddress

STATES=('open','review','closed')


@lru_cache(maxsize=1)
def private_transport():
    # Reuse only the handler configuration. urllib still closes each HTTP connection.
    return build_opener(ProxyHandler({})).open


def schedule(rate=4,duration=5,write_fraction=.2,rows=1000,seed=0):
    if not all(math.isfinite(x) for x in (rate,duration,write_fraction)) or rate<=0 or duration<=0 or not 0<=write_fraction<=1:
        raise ValueError('invalid schedule')
    count=math.floor(rate*duration)
    if type(rows) is not int or rows<100 or count>100000:raise ValueError('invalid size')
    rng=random.Random(seed);writes=set(rng.sample(range(count),round(count*write_fraction)))
    ids=iter(rng.sample(range(rows),len(writes)))
    result=[];write_index=0
    for i in range(count):
        row={'id':i,'at_s':i/rate,'kind':'write' if i in writes else 'read'}
        if i in writes:
            issue_id=next(ids);mutation='comment' if write_index%2==0 else 'state';write_index+=1
            row.update(issue_id=issue_id,mutation=mutation,value=f'New discussion {i} & follow-up' if mutation=='comment'
                else STATES[((issue_id//5)%3+1)%3])
        else:
            row.update(project_id=i%5,state=STATES[(i//5)%3],limit=5 if i%2 else 20,offset=0 if i%3 else 3)
        result.append(row)
    return result


class IssueHTML(HTMLParser):
    def __init__(self):
        super().__init__();self.main=[];self.rows=[];self.row=None;self.field=None;self.count=''
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag=='main':self.main.append(attrs)
        if tag=='article':
            self.row={'id':int(attrs['data-id']),'revision':int(attrs['data-revision']),
                'project_id':int(attrs['data-project-id']),'state':attrs['data-state'],
                'title':'','assignee':'','project_name':'','comment_count':''}
        if tag=='h2' and self.row is not None:self.field='title'
        if tag=='p' and self.row is not None:
            if 'data-assignee' in attrs:self.field='assignee'
            elif 'data-project-name' in attrs:self.field='project_name'
        if tag=='output':
            if attrs.get('id')=='issue-count':self.field='total'
            elif 'data-comment-count' in attrs:self.field='comment_count'
    def handle_data(self,value):
        if self.field=='total':self.count+=value
        elif self.row is not None and self.field:self.row[self.field]+=value
    def handle_endtag(self,tag):
        if tag in ('h2','p','output'):self.field=None
        if tag=='article' and self.row is not None:
            self.row['comment_count']=int(self.row['comment_count']);self.rows.append(self.row);self.row=None


def operation(row,strategy,app_url,origin_url,prefix,timeout=5,*,allowed_remote_host=None):
    if strategy not in ('ssr','csr'):raise ValueError('invalid strategy')
    if allowed_remote_host is None:
        if any(urlparse(url).hostname not in ('127.0.0.1','localhost') for url in (app_url,origin_url)):
            raise ValueError('loopback task endpoints required')
        transport=urlopen
    else:
        address=ipaddress.ip_address(allowed_remote_host)
        if address.version!=4 or address not in ipaddress.ip_network('100.64.0.0/10'):
            raise ValueError('private overlay IPv4 required')
        expected=f'http://{address}:8765'
        if app_url!=expected or origin_url!=expected:raise ValueError('exact selected peer endpoint required')
        transport=private_transport()
    steps=[];received=None
    def send(url,payload=None):
        nonlocal received
        received=None
        headers={'X-QRL-Operation':f'{prefix}-{row["id"]}','Cache-Control':'no-cache'}
        if payload is not None:headers.update({'X-QRL-Control':'engineering','Content-Type':'application/json'})
        request=Request(url,headers=headers,data=None if payload is None else json.dumps(payload).encode())
        try:
            with transport(request,timeout=timeout) as response:body=response.read();status=response.status
        except HTTPError as error:body=error.read();status=error.code
        received=time.perf_counter();steps.append({'status':status,'bytes':len(body)})
        return status,body
    def outcome(code,upstream=None):
        return {'outcome':'failed','failure_code':code,'upstream_status':upstream,'steps':steps,
                '_response_completed_monotonic':received}
    try:
        if row['kind']=='write':
            status,body=send(origin_url+'/control/mutate',{'operation_id':f'{prefix}-{row["id"]}',
                'issue_id':row['issue_id'],'kind':row['mutation'],'value':row['value']})
            if status!=200:return outcome('write_http_failure',status)
            ack=json.loads(body)
            if ack.get('changed') is not True or ack.get('operation_id')!=f'{prefix}-{row["id"]}' or ack.get('revision')!=1:
                return outcome('invalid_write_ack',status)
            evidence=None
        else:
            params={key:row[key] for key in ('project_id','state','limit','offset')}
            query=urlencode(params)
            status,body=send(app_url+'/issues/'+strategy+'?'+query)
            if status!=200:return outcome('page_http_failure',status)
            parser=IssueHTML();parser.feed(body.decode('utf8'))
            if len(parser.main)!=1:return outcome('invalid_page_outcome')
            main=parser.main[0]
            if main.get('data-qrl-outcome')=='failed':
                return outcome(main['data-qrl-error'],int(main['data-upstream-status']) if main['data-upstream-status'] else None)
            if main.get('data-qrl-outcome')!=('completed' if strategy=='ssr' else 'loading'):
                return outcome('invalid_page_outcome')
            if strategy=='ssr':
                evidence={'snapshot':main['data-snapshot'],'issues':parser.rows,'issuesCount':int(parser.count),**params}
            else:
                status,body=send(app_url+'/api/issues?'+query)
                data=json.loads(body)
                if status!=200 or data.get('ok') is not True:
                    return outcome(data.get('error',{}).get('code','api_http_failure'),
                                   data.get('error',{}).get('upstream_status',status))
                evidence={key:data[key] for key in ('snapshot','issues','issuesCount',*params.keys())}
        return {'outcome':'completed','steps':steps,'read_evidence':evidence,'_response_completed_monotonic':received}
    except Exception as error:
        return {'outcome':'failed','error_type':type(error).__name__,'steps':steps,'_response_completed_monotonic':received}
