from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from urllib.error import HTTPError
from urllib.request import Request,urlopen

from experiments.heldout_tasks.gateway import gateway
from experiments.heldout_tasks.runtime import reserved_runtime
from experiments.heldout_tasks.transport import TaskClient,TaskHTML
from experiments.heldout_tasks.workload import trace
from experiments.heldout_tasks.scale_reference import InventoryScaleReference,AnalyticsScaleReference
from experiments.issue_dashboard.persistent_http import OwnedSession


@unittest.skipUnless(os.environ.get('QRL_TEST_DATABASE_URL'),'disposable PostgreSQL required')
class ReservedDeliveryContracts(unittest.TestCase):
    @contextmanager
    def runtime(self,task):
        with tempfile.TemporaryDirectory(prefix='qrl-task-delivery-') as folder:
            target=SimpleNamespace(lock=threading.RLock(),runtime=None,case={'name':'correctness'},own={},remote=None)
            server=gateway(target,'127.0.0.1','127.0.0.1',0);base='http://127.0.0.1:'+str(server.server_port)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                with reserved_runtime(os.environ['QRL_TEST_DATABASE_URL'],Path(folder),task,'maintained',scale_rows=1000,public_origin=base) as value:
                    target.runtime=value;yield target,server,base
            finally:server.shutdown();server.server_close();thread.join(timeout=5)

    def test_planned_traces_are_bounded_reproducible_and_keep_browser_products_separate(self):
        for task in ('inventory','analytics'):
            for size in (1000,100000):
                for rate in (16,64):
                    for mix in (.02,.5):
                        plan=trace(task,size,rate,20,mix,123);self.assertEqual(plan,trace(task,size,rate,20,mix,123));self.assertEqual(len(plan),rate*20)
                        self.assertEqual(len({r['id'] for r in plan}),len(plan));self.assertEqual(sum(r['kind']=='write' for r in plan),round(len(plan)*mix))
                        self.assertEqual({r['mutation'] for r in plan if r['kind']=='write'},{'write_a','write_b'})
                        if task=='inventory':self.assertTrue(all(r['payload']['product_id']>20 for r in plan if r.get('mutation')=='write_a'))
                        else:
                            refunds=[r['payload'] for r in plan if r.get('mutation')=='write_b']
                            self.assertTrue(all(100+int(r['sale_id'].split('-')[-1])<=r['event_time_s']<700 for r in refunds))
                            self.assertTrue(any(100<=r['event_time_s']<200 for r in refunds))
        with self.assertRaises(ValueError):trace('inventory',1000,128,20,.5,1)

    def test_owned_ssr_csr_mixed_business_work_replays_without_performance_output(self):
        for task in ('inventory','analytics'):
            for strategy in ('ssr','csr'):
                with self.subTest(task=task,strategy=strategy),self.runtime(task) as (target,server,base):
                    session=OwnedSession('127.0.0.1',server.server_port,persistent=True);client=TaskClient(session,task,strategy,'correctness-'+task+'-'+strategy)
                    try:results=[client.invoke({'id':'default','kind':'read'}),*[client.invoke(item) for item in trace(task,1000,1,60,.4,456)]]
                    finally:session.close()
                    self.assertEqual({r['outcome'] for r in results},{'completed'})
                    self.assertTrue(all(r['_response_completed_monotonic'] is None for r in results));self.assertTrue(all('body_receipt_ns' not in r for r in session.records))
                    target.runtime['quiesce']();state=target.runtime['fixture'].validation_state();reference=(InventoryScaleReference if task=='inventory' else AnalyticsScaleReference)(state);reference.verify_final()
                    for row in results:
                        if row['read_evidence'] is not None:reference.verify_query(row['read_evidence']);self.assertEqual(len(row['steps']),1 if strategy=='ssr' else 2)
                    receipts={(r['server_connection_id'],r['operation_id'],r['path']):r for r in session.records}
                    events=target.connection_observations;self.assertEqual(len(receipts),len(events))
                    for event in events:
                        match=receipts[event['connection_id'],event['operation_id'],next(r['path'] for r in session.records if r['server_connection_id']==event['connection_id'] and r['operation_id']==event['operation_id'] and r['path'].split('?')[0]==event['path'])]
                        self.assertEqual(match['body_sha256'],event['body_sha256']);self.assertNotEqual(event['tcp_nodelay_raw'],0)
                    self.assertFalse(target.errors);self.assertFalse(target.proxy_transport_failures)

    def test_public_origin_guard_and_document_query_identity(self):
        with self.runtime('inventory') as (target,server,base):
            payload={'operation_id':'browser-business','kind':'reserve','reservation_id':'browser-business','product_id':1,'quantity':1}
            def post(origin):
                headers={'Content-Type':'application/json','X-QRL-Operation':'browser-business'}
                if origin is not None:headers['Origin']=origin
                return urlopen(Request(base+'/api/task/mutate',data=json.dumps(payload).encode(),headers=headers),timeout=5)
            for origin in (None,'http://unrelated.invalid'):
                with self.assertRaises(HTTPError) as error:post(origin)
                self.assertEqual(error.exception.code,400)
            with post(base) as response:self.assertEqual(json.load(response)['outcome'],'reserved')
            with urlopen(base+'/task/ssr?operation_id=document-from-query',timeout=5) as response:self.assertIn(b'Product 2',response.read())
            identities=[r for r in target.identity_observations if r['operation_id']=='document-from-query'];self.assertEqual(identities[0]['source'],'document_query_parameter')
            req=Request(target.runtime['origin'].url+'/control/mutate',data=json.dumps(payload).encode(),headers={'Content-Type':'application/json','X-QRL-Control':'engineering','Origin':base})
            with self.assertRaises(HTTPError) as error:urlopen(req,timeout=5)
            self.assertEqual(error.exception.code,403)

    def test_visible_text_corruption_is_rejected_with_unchanged_data_attributes(self):
        with self.runtime('inventory') as (target,server,base):
            req=Request(base+'/task/ssr',headers={'X-QRL-Operation':'text-check'})
            with urlopen(req,timeout=5) as response:raw=response.read().decode()
            good=TaskHTML();good.feed(raw);self.assertEqual(good.result('inventory','ssr')['items'][0]['remaining_stock'],1)
            self.assertIn('Available: 1',raw)
            for original,replacement in [('Available: 1','Available: 999'),('Products: 800','Products: 999')]:
                self.assertIn(original,raw);bad=TaskHTML();bad.feed(raw.replace(original,replacement,1))
                with self.assertRaises(ValueError):bad.result('inventory','ssr')


if __name__=='__main__':unittest.main()
