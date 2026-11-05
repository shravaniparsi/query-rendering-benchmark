"""Optional client-observed phase clocks; no SQL or transaction semantic changes."""
import time


class WritePhases:
    def __init__(self,started=None,clock=time.perf_counter):
        self.clock=clock;self.started=clock() if started is None else started;self.records=[]

    def call(self,name,function,*args,**kwargs):
        before=self.clock()
        try:return function(*args,**kwargs)
        finally:
            after=self.clock()
            self.records.append({'name':name,'start_ms':(before-self.started)*1000,
                                 'end_ms':(after-self.started)*1000,'wall_ms':(after-before)*1000})

    def transaction(self,context):
        trace=self
        class TimedTransaction:
            def __enter__(self):return trace.call('begin',context.__enter__)
            def __exit__(self,*exception):return trace.call('commit_exit' if exception[0] is None else 'rollback_exit',context.__exit__,*exception)
        return TimedTransaction()

    def finish(self,wall_ms):
        total=sum(r['wall_ms'] for r in self.records)
        return {'format':'issue-write-phases-v1','phases':self.records,'phase_sum_ms':total,
                'residual_ms':wall_ms-total}


def expected_phases(query_kind,mutation):
    result=['begin','operation_lock','retry_lookup','issue_lock']
    if mutation=='comment':
        result+=['comment_insert']+(['count_update'] if query_kind=='maintained' else [])+['issue_revision_update']
    else:result+=['issue_state_update']
    return result+['ledger_insert','commit_exit']
