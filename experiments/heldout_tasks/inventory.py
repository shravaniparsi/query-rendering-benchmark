"""Ledger-derived reference for reserved inventory-task correctness engineering.

This is an independent Python semantic oracle, not the database application or a
performance fixture. Availability is recomputed from active reservations rather
than maintained in a stock counter. No timing observations are collected.
"""
from collections import Counter
from copy import deepcopy
import threading


def require(value,message):
    if not value:raise ValueError(message)


def integer(value,minimum=0):return type(value) is int and value>=minimum


class InventoryOracle:
    def __init__(self,products):
        require(products,'products required');self.products={};self.reservations={};self.operations={};self.lock=threading.Lock()
        for product in products:
            require(set(product)=={'id','category','brand','price_minor','initial_stock'},'product schema differs')
            require(integer(product['id'],1) and product['id'] not in self.products,'product identity differs')
            require(all(isinstance(product[k],str) and product[k] for k in ('category','brand')),'facet values required')
            require(integer(product['price_minor']) and integer(product['initial_stock']),'integer stock/money required')
            self.products[product['id']]=deepcopy(product)
    def _remaining(self,product_id):
        return self.products[product_id]['initial_stock']-sum(r['quantity'] for r in self.reservations.values() if r['product_id']==product_id and r['status']=='active')
    def _check(self):
        require(all(self._remaining(i)>=0 for i in self.products),'negative stock')
        require(all(r['status'] in ('active','cancelled') and r['product_id'] in self.products and integer(r['quantity'],1) for r in self.reservations.values()),'invalid reservation')
    def apply(self,operation_id,kind,reservation_id,*,product_id=None,quantity=None):
        require(isinstance(operation_id,str) and operation_id and isinstance(reservation_id,str) and reservation_id,'operation/reservation identity required')
        require(kind in ('reserve','cancel'),'unknown operation')
        if kind=='reserve':require(integer(product_id,1) and product_id in self.products and integer(quantity,1),'invalid reservation request')
        else:require(product_id is None and quantity is None,'cancel payload differs')
        payload={'kind':kind,'reservation_id':reservation_id,'product_id':product_id,'quantity':quantity}
        with self.lock:
            if operation_id in self.operations:
                old=self.operations[operation_id];require(old['request']==payload,'idempotency identity reused with different payload')
                return deepcopy(old['result'])
            if kind=='reserve':
                if reservation_id in self.reservations:outcome='reservation_exists'
                elif self._remaining(product_id)<quantity:outcome='insufficient_stock'
                else:self.reservations[reservation_id]={'product_id':product_id,'quantity':quantity,'status':'active'};outcome='reserved'
            else:
                reservation=self.reservations.get(reservation_id)
                if reservation is None:outcome='not_found'
                elif reservation['status']=='cancelled':outcome='already_cancelled'
                else:reservation['status']='cancelled';outcome='cancelled'
            result={'operation_id':operation_id,'reservation_id':reservation_id,'outcome':outcome,'changed':outcome in ('reserved','cancelled')}
            self.operations[operation_id]={'request':payload,'result':result};self._check();return deepcopy(result)
    def state(self):
        with self.lock:
            self._check()
            return {'products':[{**deepcopy(p),'remaining_stock':self._remaining(i)} for i,p in sorted(self.products.items())],
                'reservations':[{'reservation_id':i,**deepcopy(r)} for i,r in sorted(self.reservations.items())],
                'operations':[{'operation_id':i,**deepcopy(r)} for i,r in sorted(self.operations.items())]}
    def query(self,*,category=None,brand=None,min_price_minor=0,max_price_minor=None,available_only=True,offset=0,limit=20):
        require(category is None or isinstance(category,str),'invalid category');require(brand is None or isinstance(brand,str),'invalid brand')
        require(integer(min_price_minor) and (max_price_minor is None or integer(max_price_minor) and max_price_minor>=min_price_minor),'integer price interval required')
        require(type(available_only) is bool and integer(offset) and integer(limit,1) and limit<=100,'invalid page/availability request')
        # One lock acquisition supplies rows, exact count and conjunctive facets from one snapshot.
        with self.lock:
            rows=[{**deepcopy(p),'remaining_stock':self._remaining(i)} for i,p in sorted(self.products.items())]
            rows=[r for r in rows if (category is None or r['category']==category) and (brand is None or r['brand']==brand)
                and r['price_minor']>=min_price_minor and (max_price_minor is None or r['price_minor']<=max_price_minor)
                and (not available_only or r['remaining_stock']>0)]
            return {'items':rows[offset:offset+limit],'itemsCount':len(rows),
                'facets':{key:dict(sorted(Counter(r[key] for r in rows).items())) for key in ('category','brand')}}
    def verify_state(self,observed):
        require(observed==self.state(),'inventory state differs from independent reservation ledger');return True
