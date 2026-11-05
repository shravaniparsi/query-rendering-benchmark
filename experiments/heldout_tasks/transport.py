"""Owned logical reserved-task work and actual SSR text consistency checks."""
import json
from html.parser import HTMLParser
from urllib.parse import urlencode
from .inventory import require


def money(value):
    value=int(value);magnitude=abs(value)
    return ('-' if value<0 else '')+'$'+str(magnitude//100)+'.'+str(magnitude%100).zfill(2)


class TaskHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True);self.main=[];self.articles=[];self.current=None;self.part=None;self.script=None
        self.scripts={};self.count=[];self.counting=False;self.facet=None;self.span=None;self.facets={}
    def handle_starttag(self,tag,attributes):
        attrs=dict(attributes)
        if tag=='main':self.main.append(attrs)
        if tag=='script':
            self.script=attrs.get('id')
            if self.script in ('initial-data','parameters'):
                require(self.script not in self.scripts,'duplicate application JSON');self.scripts[self.script]=[]
        if tag=='article':require(self.current is None,'nested article');self.current={'attributes':attrs,'title':[],'value':[]}
        if self.current is not None and tag in ('h2','output'):self.part='title' if tag=='h2' else 'value'
        if tag=='p' and attrs.get('id')=='task-count':self.counting=True
        if tag=='p' and attrs.get('data-facet'):
            self.facet=attrs['data-facet'];require(self.facet not in self.facets,'duplicate facet');self.facets[self.facet]={}
        if tag=='span' and self.facet:
            self.span={'attributes':attrs,'text':[]}
    def handle_data(self,data):
        if self.script in self.scripts:self.scripts[self.script].append(data)
        if self.current is not None and self.part:self.current[self.part].append(data)
        if self.counting:self.count.append(data)
        if self.span is not None:self.span['text'].append(data)
    def handle_endtag(self,tag):
        if tag=='script':self.script=None
        if tag in ('h2','output'):self.part=None
        if tag=='article':
            require(self.current is not None,'article end differs');self.articles.append(self.current);self.current=None
        if tag=='span' and self.span is not None:
            attrs=self.span['attributes'];name=attrs['data-value'];count=int(attrs['data-count'])
            require(name not in self.facets[self.facet] and ''.join(self.span['text']).strip()==f'{name} ({count})','visible facet differs')
            self.facets[self.facet][name]=count;self.span=None
        if tag=='p':self.counting=False;self.facet=None
    def result(self,task,strategy):
        require(len(self.main)==1 and self.main[0].get('data-task')==task and self.main[0].get('data-strategy')==strategy,'application DOM identity differs')
        main=self.main[0];value=json.loads(''.join(self.scripts['initial-data']))
        if strategy=='csr':
            require(value is None and not self.articles and main['data-qrl-outcome']=='loading','CSR shell differs');return None
        if main['data-qrl-outcome']=='failed':return {'ok':False,**(value or {})}
        require(value['ok'] is True and main['data-snapshot']==value['snapshot'] and main['data-response-operation']==value['read_operation'],'SSR snapshot identity differs')
        rows=value['items' if task=='inventory' else 'groups'];require(len(rows)==len(self.articles),'SSR page length differs')
        expected_count=f"Products: {value['itemsCount']}" if task=='inventory' else f"Groups: {value['groupsCount']} · Events: {value['eventCount']}"
        require(''.join(self.count).strip()==expected_count,'visible count differs')
        if task=='inventory':require(self.facets==value['facets'],'visible facets differ')
        for actual,row in zip(self.articles,rows):
            attrs=actual['attributes']
            if task=='inventory':
                fields=('id','category','brand','price_minor','initial_stock','remaining_stock')
                require(all(attrs['data-'+key.replace('_','-')]==str(row[key]) for key in fields),'SSR product fields differ')
                title=f"Product {row['id']}";text=f"Available: {row['remaining_stock']}"
            else:
                fields=(*value['parameters']['group_by'],'gross_total_minor','refund_total_minor','sales_count','refund_count','net_total_minor')
                require(all(attrs['data-'+key.replace('_','-')]==str(row[key]) for key in fields),'SSR analytics fields differ')
                title=' / '.join(row[key] for key in value['parameters']['group_by']);text='Net sales: '+money(row['net_total_minor'])
            require(''.join(actual['title']).strip()==title and ''.join(actual['value']).strip()==text,'visible title/value differs')
        return value


class TaskClient:
    def __init__(self,session,task,strategy,prefix):
        require(task in ('inventory','analytics') and strategy in ('ssr','csr') and isinstance(prefix,str) and len(prefix)<=65,'bounded task/client identity required')
        self.session,self.task,self.strategy,self.prefix=session,task,strategy,prefix
    def invoke(self,item):
        steps=[];received=None;evidence=ack=None;operation=self.prefix+'-'+str(item['id'])
        def send(path,payload=None):
            nonlocal received
            row,raw=self.session.request('GET' if payload is None else 'POST',path,operation_id=operation,body=None if payload is None else json.dumps(payload).encode())
            if row.get('body_receipt_ns') is not None:received=int(row['body_receipt_ns'])/1e9
            steps.append({'status':row['status'],'bytes':row['body_bytes'],'path':path,'server_connection_id':row['server_connection_id'],'outcome':row['outcome']});return row,raw
        def failed(code,status=None):return {'outcome':'failed','failure_code':code,'upstream_status':status,'steps':steps,'read_evidence':None,'acknowledgement':None,'_response_completed_monotonic':received}
        try:
            if item['kind']=='write':
                name='operation_id' if self.task=='inventory' else 'event_id';payload={**item['payload'],name:operation}
                row,raw=send('/control/mutate',payload)
                if row['outcome']!='completed':return failed('network_failure')
                if row['status']!=200:return failed('write_http_failure',row['status'])
                ack=json.loads(raw);require(ack.get('ok') is True and ack.get(name)==operation and ack.get('changed') is True,'business acknowledgement differs')
            else:
                parameters=item.get('parameters',{} if self.task=='inventory' else {'start_s':100,'end_s':200})
                query=urlencode({'parameters':json.dumps(parameters)});row,raw=send('/task/'+self.strategy+'?'+query)
                if row['outcome']!='completed':return failed('network_failure')
                if row['status']!=200:return failed('page_http_failure',row['status'])
                page=TaskHTML();page.feed(raw.decode());evidence=page.result(self.task,self.strategy)
                if self.strategy=='csr':
                    row,raw=send('/api/task?'+query)
                    if row['outcome']!='completed':return failed('network_failure')
                    evidence=json.loads(raw)
                if not evidence.get('ok'):
                    error=evidence.get('error',{});return failed(error.get('code','api_http_failure'),error.get('upstream_status',row['status']))
                require(row['status']==200 and evidence['read_operation']=={'inventory':'inventory_page','analytics':'event_time_groups'}[self.task],'task read operation differs')
            return {'outcome':'completed','steps':steps,'read_evidence':evidence,'acknowledgement':ack,'_response_completed_monotonic':received}
        except (ValueError,KeyError,TypeError,UnicodeError) as error:
            return {'outcome':'integrity_failure','error_type':type(error).__name__,'steps':steps,'read_evidence':evidence,'acknowledgement':ack,'_response_completed_monotonic':received}
