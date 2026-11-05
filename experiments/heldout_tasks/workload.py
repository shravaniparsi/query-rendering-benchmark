"""Finite reserved-task trace specification, without acquisition or timing."""
import random
from .inventory import require
from .scale_sql import size

VERSION='reserved-workload-trace-v1.1'
APPLICATIONS={'inventory':'inventory-reservation','analytics':'time-window-analytics'}


def read_parameters(task,index):
    if task=='inventory':
        return ({},{'category':'category-1','brand':'brand-2','available_only':False,'offset':2,'limit':5},
            {'brand':'brand-2','offset':4,'limit':10},{'brand':'absent'})[index%4].copy()
    return ({'start_s':100,'end_s':200},{'start_s':100,'end_s':3700},
        {'start_s':99,'end_s':100},{'start_s':100,'end_s':700,'region':'region-1','group_by':['region'],'limit':1})[index%4].copy()


def trace(task,rows,rate,duration_s,write_fraction,seed):
    require(task in APPLICATIONS,'unknown reserved task');size(rows)
    require(type(rate) is int and rate in (1,16,64) and type(duration_s) is int and 1<=duration_s<=60 and
        write_fraction in (.02,.4,.5) and type(seed) is int,'bounded declared workload required')
    total=rate*duration_s;writes=round(total*write_fraction);a=writes//2;b=writes-a
    require(a>0 and b>0,'both write classes required');kinds=['write_a']*a+['write_b']*b+['read']*(total-writes)
    random.Random(seed).shuffle(kinds);counts={'read':0,'write_a':0,'write_b':0};items=[]
    products=[i for i in range(21,rows+1) if i%5>0]
    require(max(a,b)<=len(products),'trace would reuse mutable background identities')
    for identity,kind in enumerate(kinds):
        index=counts[kind];counts[kind]+=1;item={'id':identity,'at_s':identity/rate,'kind':'read' if kind=='read' else 'write'}
        if kind=='read':item['parameters']=read_parameters(task,index)
        else:
            item['mutation']=kind
            if task=='inventory':
                product=products[index]
                item['payload']=({'kind':'reserve','reservation_id':'trace-reservation-'+str(index),'product_id':product,'quantity':1} if kind=='write_a' else
                    {'kind':'cancel','reservation_id':'seed-reservation-'+str(product)})
            else:
                item['payload']=({'kind':'sale','event_time_s':99 if index%2==0 else 100+index%100,'amount_minor':100,
                    'region':'region-'+str(index%3),'category':'category-'+str(index%4)} if kind=='write_a' else
                    {'kind':'refund','event_time_s':100+index%3600+(60 if index%2 else 0),'amount_minor':100,'sale_id':'seed-sale-'+str(index)})
        items.append(item)
    return items
