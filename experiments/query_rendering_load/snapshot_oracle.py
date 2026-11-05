"""Offline read oracle for the declared seeded, unique-entity update workload.

Standard-library only: no live database or service query implementation required.
"""
import argparse
import json
from pathlib import Path

TOPICS=('rendering','accessibility','databases','systems')


def parse_snapshot(value):
    parts=value.split(':')
    if len(parts)!=3:
        raise ValueError('invalid snapshot format')
    xmin,xmax=int(parts[0]),int(parts[1])
    xip=tuple(int(x) for x in parts[2].split(',') if x)
    if xmin<0 or xmax<xmin or len(set(xip))!=len(xip) or any(x<xmin or x>=xmax for x in xip):
        raise ValueError('invalid snapshot bounds')
    return xmin,xmax,set(xip)


def visible(xid,snapshot):
    xmin,xmax,xip=parse_snapshot(snapshot)
    xid=int(xid)
    if xid<=0:
        raise ValueError('invalid committed xid')
    return xid<xmin or (xid<xmax and xid not in xip)


class ReadOracle:
    def __init__(self,state):
        self.size=state['seed_rows']; self.ledger=state['ledger']; self.cache={}
        if type(self.size) is not int or not 40<=self.size<=1000000:
            raise ValueError('invalid seed size')
        ids=set(); operations=set()
        for row in self.ledger:
            if (row['article_id'] in ids or row['operation_id'] in operations or
                    type(row['article_id']) is not int or not 0<=row['article_id']<self.size or
                    row['revision']!=1 or row['field'] not in ('tag','title') or
                    not isinstance(row['value'],str) or int(row['transaction_xid'])<=0):
                raise ValueError('ledger violates unique-entity workload')
            if row['field']=='tag' and row['value'] not in TOPICS:
                raise ValueError('invalid tag update')
            ids.add(row['article_id']); operations.add(row['operation_id'])

    def expected(self,snapshot,tag,offset=0,limit=20):
        if tag not in TOPICS or type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=100:
            raise ValueError('invalid page contract')
        active=tuple(index for index,row in enumerate(self.ledger) if visible(row['transaction_xid'],snapshot))
        key=(active,tag,offset,limit)
        if key not in self.cache:
            changes={self.ledger[index]['article_id']:self.ledger[index] for index in active}
            count=0; articles=[]
            for article_id in reversed(range(self.size)):
                row=changes.get(article_id)
                actual_tag=row['value'] if row and row['field']=='tag' else TOPICS[article_id%4]
                if actual_tag!=tag:
                    continue
                if offset<=count<offset+limit:
                    articles.append({'id':article_id,'title':row['value'] if row and row['field']=='title' else f'Engineering note {article_id:07d}',
                                     'tag':actual_tag,'revision':1 if row else 0,'author':f'Author {article_id%20:02d}'})
                count+=1
            self.cache[key]={'articles':articles,'articlesCount':count}
        return self.cache[key]

    def verify(self,evidence):
        expected=self.expected(evidence['snapshot'],evidence['tag'],evidence['offset'],evidence['limit'])
        if type(evidence.get('articlesCount')) is not int or evidence['articlesCount']!=expected['articlesCount']:
            raise ValueError('response count differs from snapshot oracle')
        projected=[{key:row[key] for key in ('id','title','tag','revision','author')} for row in evidence['articles']]
        if any(type(row['id']) is not int or type(row['revision']) is not int for row in projected):
            raise ValueError('invalid row field types')
        if projected!=expected['articles']:
            raise ValueError('response content/order/revision differs from snapshot oracle')


def audit_directory(directory):
    directory=Path(directory)
    state=json.loads((directory/'validation-state.json').read_text())
    records=json.loads((directory/'operations.json').read_text())
    oracle=ReadOracle(state); checked=0; errors=[]
    for row in records:
        if row['kind']=='read' and row['outcome']=='completed':
            try:
                oracle.verify(row['read_evidence']); checked+=1
            except (ValueError,KeyError,TypeError) as error:
                errors.append({'operation_id':row['id'],'error':str(error)})
    return {'format':'qrl-read-oracle-v1','status':'failed' if errors else 'passed',
            'completed_reads_checked':checked,'errors':errors,'distinct_visibility_pages':len(oracle.cache)}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory',type=Path)
    result=audit_directory(parser.parse_args().directory)
    print(json.dumps(result,indent=2))
    raise SystemExit(result['status']!='passed')
