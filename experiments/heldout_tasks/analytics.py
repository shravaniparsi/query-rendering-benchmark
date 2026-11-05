"""Independent event-ledger reference for reserved time-window analytics.

Correctness-only: integer monetary arithmetic and event-time windows; no query
implementation, SQL, browser, comparative timings or selector training.
"""
from collections import defaultdict
from copy import deepcopy
import threading
from .inventory import integer,require


class AnalyticsOracle:
    def __init__(self):self.events=[];self.operations={};self.sales={};self.lock=threading.Lock()
    def append(self,event_id,kind,event_time_s,amount_minor,*,sale_id=None,region=None,category=None):
        require(isinstance(event_id,str) and event_id and kind in ('sale','refund'),'event identity/kind differs')
        require(integer(event_time_s) and integer(amount_minor,1),'integer event time and positive minor-unit amount required')
        if kind=='sale':require(sale_id is None and all(isinstance(x,str) and x for x in (region,category)),'sale dimensions required')
        else:require(isinstance(sale_id,str) and sale_id and region is None and category is None,'refund must inherit sale dimensions')
        request={'event_id':event_id,'kind':kind,'event_time_s':event_time_s,'amount_minor':amount_minor,'sale_id':sale_id,'region':region,'category':category}
        with self.lock:
            if event_id in self.operations:
                previous=self.operations[event_id];require(previous['request']==request,'event identity reused with different payload');return deepcopy(previous['result'])
            outcome='recorded';event=deepcopy(request)
            if kind=='refund':
                sale=self.sales.get(sale_id)
                refunded=sum(e['amount_minor'] for e in self.events if e['kind']=='refund' and e['sale_id']==sale_id)
                if sale is None:outcome='sale_not_found'
                elif event_time_s<sale['event_time_s']:outcome='refund_precedes_sale'
                elif refunded+amount_minor>sale['amount_minor']:outcome='refund_exceeds_sale'
                else:event.update(region=sale['region'],category=sale['category'])
            if outcome=='recorded':
                event['ingest_sequence']=len(self.events);self.events.append(event)
                if kind=='sale':self.sales[event_id]=event
            result={'event_id':event_id,'outcome':outcome,'changed':outcome=='recorded'}
            self.operations[event_id]={'request':request,'result':result};return deepcopy(result)
    def query(self,start_s,end_s,*,group_by=('region','category'),region=None,category=None,offset=0,limit=20):
        require(integer(start_s) and integer(end_s) and start_s<end_s,'nonempty half-open event-time window required')
        require(group_by in (('region',),('category',),('region','category')),'unsupported grouping')
        require(all(x is None or isinstance(x,str) for x in (region,category)) and integer(offset) and integer(limit,1) and limit<=100,'invalid filters/page')
        with self.lock:
            # Window membership uses event time, never ingestion order. Late arrivals are included.
            selected=[e for e in self.events if start_s<=e['event_time_s']<end_s and (region is None or e['region']==region) and (category is None or e['category']==category)]
            groups=defaultdict(lambda:{'gross_total_minor':0,'refund_total_minor':0,'sales_count':0,'refund_count':0})
            for event in selected:
                row=groups[tuple(event[k] for k in group_by)];sale=event['kind']=='sale'
                row['gross_total_minor' if sale else 'refund_total_minor']+=event['amount_minor'];row['sales_count' if sale else 'refund_count']+=1
            rows=[{**dict(zip(group_by,key)),**value,'net_total_minor':value['gross_total_minor']-value['refund_total_minor']} for key,value in groups.items()]
            rows.sort(key=lambda r:(-r['net_total_minor'],*(r[k] for k in group_by)))
            return {'groups':rows[offset:offset+limit],'groupsCount':len(rows),'eventCount':len(selected),'window':{'start_s':start_s,'end_s':end_s,'bounds':'[start,end)'}}
    def state(self):
        with self.lock:return {'events':deepcopy(self.events),'operations':[{'event_id':i,**deepcopy(r)} for i,r in sorted(self.operations.items())]}
    def verify_state(self,observed):require(observed==self.state(),'analytics state differs from independent event ledger');return True
