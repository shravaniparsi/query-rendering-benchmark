"""Independent seed/ledger reconstruction for issue pages. Standard library only."""
from collections import defaultdict

from experiments.query_rendering_load.snapshot_oracle import parse_snapshot,visible

STATUSES = ('open','review','closed')


class IssueOracle:
    def __init__(self,state):
        self.rows,self.fanout,self.ledger = state['rows'],state['fanout'],state['ledger']
        if type(self.rows) is not int or not 100<=self.rows<=100000 or type(self.fanout) is not int or not 0<=self.fanout<=200:
            raise ValueError('invalid seeded dimensions')
        operations,comments = set(),set()
        self.by_issue = defaultdict(list)
        for row in self.ledger:
            if (not isinstance(row['operation_id'],str) or row['operation_id'] in operations or
                type(row['issue_id']) is not int or not 0<=row['issue_id']<self.rows or
                type(row['revision']) is not int or row['revision']<1 or row['kind'] not in ('state','comment') or
                not isinstance(row['value'],str) or not row['value'] or int(row['transaction_xid'])<=0):
                raise ValueError('invalid committed mutation ledger')
            if row['kind']=='state':
                if row['value'] not in STATUSES or row['comment_id'] is not None:
                    raise ValueError('invalid state mutation')
            else:
                if (type(row['comment_id']) is not int or row['comment_id']<=self.rows*(self.fanout+1) or
                    row['comment_id'] in comments):
                    raise ValueError('invalid appended comment identity')
                comments.add(row['comment_id'])
            operations.add(row['operation_id'])
            self.by_issue[row['issue_id']].append(row)
        for rows in self.by_issue.values():
            rows.sort(key=lambda r:r['revision'])
            if [r['revision'] for r in rows]!=list(range(1,len(rows)+1)):
                raise ValueError('issue revisions are not a contiguous committed sequence')
        self.cache = {}

    def issue_at(self,issue_id,snapshot=None):
        changes = [r for r in self.by_issue.get(issue_id,()) if snapshot is None or visible(r['transaction_xid'],snapshot)]
        if [r['revision'] for r in changes]!=list(range(1,len(changes)+1)):
            raise ValueError('snapshot sees a later issue revision without its predecessor')
        state = STATUSES[(issue_id//5)%3]
        comments = issue_id%(self.fanout+1)
        for row in changes:
            if row['kind']=='state': state=row['value']
            else: comments+=1
        return {'id':issue_id,'project_id':issue_id%5,'project_name':f'Project {issue_id%5}',
                'title':f'Issue {issue_id:07d} & discussion','state':state,'revision':len(changes),
                'assignee':f'Engineer {issue_id%17:02d}','comment_count':comments}

    def expected(self,snapshot,project_id,state,limit=20,offset=0):
        parse_snapshot(snapshot)
        if type(project_id) is not int or not 0<=project_id<5 or state not in STATUSES:
            raise ValueError('invalid project/state filter')
        if type(limit) is not int or not 1<=limit<=100 or type(offset) is not int or offset<0:
            raise ValueError('invalid page dimensions')
        # Visibility signature shares cache entries when no committed mutation changes.
        active = tuple(r['operation_id'] for r in self.ledger if visible(r['transaction_xid'],snapshot))
        key = active,project_id,state,limit,offset
        if key not in self.cache:
            rows=[];count=0
            for issue_id in range(self.rows-1,-1,-1):
                if issue_id%5!=project_id: continue
                row = self.issue_at(issue_id,snapshot)
                if row['state']!=state: continue
                if offset<=count<offset+limit: rows.append(row)
                count+=1
            self.cache[key] = {'issues':rows,'issuesCount':count}
        return self.cache[key]

    def verify(self,evidence):
        expected = self.expected(evidence['snapshot'],evidence['project_id'],evidence['state'],
                                 evidence['limit'],evidence['offset'])
        if type(evidence['issuesCount']) is not int or evidence['issuesCount']!=expected['issuesCount']:
            raise ValueError('rendered issue count differs from independent snapshot')
        keys = ('id','project_id','project_name','title','state','revision','assignee','comment_count')
        actual = [{key:row[key] for key in keys} for row in evidence['issues']]
        if any(type(row[key]) is not int for row in actual for key in ('id','project_id','revision','comment_count')):
            raise ValueError('invalid issue numeric field')
        if actual!=expected['issues']:
            raise ValueError('issue contents/order/aggregates differ from independent snapshot')

    def verify_final(self,state):
        changed = state['changed_issues']
        expected = [{key:row[key] for key in ('id','state','revision')}
                    for row in (self.issue_at(i) for i in sorted(self.by_issue)) if row['revision']>0]
        if changed!=expected:
            raise ValueError('final issue state differs from committed mutations')
        appended = sorted(({'id':r['comment_id'],'issue_id':r['issue_id'],'body':r['value']}
                           for r in self.ledger if r['kind']=='comment'),key=lambda r:r['id'])
        if state['appended_comments']!=appended:
            raise ValueError('appended comments differ from ledger')
        if state['query_kind']=='maintained':
            summaries = [{'issue_id':i,'comment_count':self.issue_at(i)['comment_count']} for i in range(self.rows)]
            if state['summary_counts']!=summaries:
                raise ValueError('maintained counts differ from independent aggregation')
        elif state['summary_counts'] is not None:
            raise ValueError('unexpected summary state for an on-read query')
        return {'status':'passed','committed_mutations':len(self.ledger),'appended_comments':len(appended),
                'changed_issues':len(changed),'summary_counts_checked':self.rows if state['query_kind']=='maintained' else 0}
