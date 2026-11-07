"""Synthetic clock/profile corruption tests; no reserved performance acquisition."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from experiments.heldout_tasks.performance_evidence import derive_profile,verify_operations,verify_browser,verify_document
from experiments.heldout_tasks.performance_observer import daily_choices
from experiments.issue_dashboard.endtoend_transport import POLICY
from test_main_design import artifacts

ROOT=Path(__file__).resolve().parents[1]


def serial_input(rows,query,renderer):
    name=f's-{rows}-{query}-{renderer}';records=[];events=[]
    for i in range(60):
        kind='read' if i<36 else 'write_a' if i<48 else 'write_b'
        records.append({'id':i,'kind':'read' if i<36 else 'write','mutation':kind,'outcome':'completed',
            'steps':[{'bytes':100}]*(1 if renderer=='ssr' or kind!='read' else 2),'read_evidence':{'items':[1,2]}})
        events.append({'operation_id':name+'-main-'+str(i),'backend':{'transaction_wall_ms':1+i/100}})
    return {'case':{'name':name,'phase':'serial','task':'inventory','candidate':query,'rows':rows,'strategy':renderer},'packet':{'backend_events':events},
        'bundle':{'operations':records},'browser_report':{'synthetic':True},'audit':{'integrity_verified':True,'ambiguous_writes':[],
            'browser':{'eligible':True,'probes':[{'critical_content_paint_ms':10+i} for i in range(3)]}}}


class MainEvidenceContracts(unittest.TestCase):
    def test_ssr_document_default_parameters_match_the_visible_inventory_query(self):
        normalized={'category':None,'brand':None,'min_price_minor':0,'max_price_minor':None,'available_only':True,'offset':0,'limit':20}
        evidence={'snapshot':'1:2:','read_operation':'inventory_page','parameters':normalized,'items':[],'itemsCount':0,'facets':{'brand':{},'category':{}}}
        html='<main data-task="inventory" data-strategy="ssr" data-qrl-outcome="completed" data-snapshot="1:2:" data-response-operation="inventory_page"></main><p id="task-count">Products: 0</p><p data-facet="brand"></p><p data-facet="category"></p><script id="initial-data">'+json.dumps({'ok':True,**evidence})+'</script>'
        dom={**evidence,'parameters':{}}
        self.assertTrue(verify_document('inventory','ssr',html,dom))
        with self.assertRaises(ValueError):verify_document('inventory','ssr',html,{**dom,'parameters':{'available_only':False}})
        with self.assertRaises(ValueError):verify_document('inventory','ssr',html.replace('Products: 0','Products: 9'),dom)

    def test_serial_feature_derivation_and_loaded_phase_rejection(self):
        selected={r:serial_input(1000,'maintained',r) for r in ('ssr','csr')};value=derive_profile('inventory','maintained',1000,'1'*40,selected)
        self.assertEqual(value['status'],'usable');self.assertEqual(value['native_samples'],{'read':72,'write_a':24,'write_b':24})
        features=value['profile']['features'];self.assertEqual(features['result_rows'],2);self.assertEqual(features['read_payload_bytes'],100);self.assertEqual(features['csr_serial_paint_median_ms'],11)
        changed=deepcopy(selected);changed['csr']['packet']['loaded_results']={'best_action':'batched/csr','latency_ms':999999}
        self.assertEqual(features,derive_profile('inventory','maintained',1000,'1'*40,changed)['profile']['features'])
        changed['csr']['case']['phase']='loaded'
        with self.assertRaises(ValueError):derive_profile('inventory','maintained',1000,'1'*40,changed)

    def test_one_unusable_profile_keeps_all_eight_contexts_and_every_policy(self):
        value=artifacts();value['model']=json.loads((ROOT/'data/decision-artifact/development-v1-1/model.json').read_bytes())
        value['profile_protocol']['performance_profile_fallbacks']={'inventory-reservation':'maintained/ssr','time-window-analytics':'maintained/ssr'}
        selected=[serial_input(n,q,r) for n in (1000,100000) for q in ('correlated','batched','maintained') for r in ('ssr','csr')]
        profiles,choices=daily_choices(value,'inventory','1'*40,selected);self.assertEqual(len(profiles['profiles']),8);self.assertEqual(choices['joint_decisions'],216)
        selected[0]['bundle']['operations'][0]['outcome']='failed';profiles,choices=daily_choices(value,'inventory','1'*40,selected)
        self.assertFalse(profiles['profile_status']['inventory-reservation']['usable']);self.assertEqual(choices['fallback_decisions'],216)
        for policy in choices['choices'].values():self.assertEqual(len(policy),8);self.assertTrue(all(actions==['maintained/ssr']*27 for actions in policy.values()))
        selected.pop()
        with self.assertRaises(ValueError):daily_choices(value,'inventory','1'*40,selected)

    def test_receipt_clock_and_missing_offer_corruptions_are_detected(self):
        receipt={'policy':POLICY,'persistent':True,'attempts':1,'allowed_cpus':[0],'outcome':'completed','validation_finish_ns':'101001000000','body_receipt_ns':'101000000000',
            'http_version':11,'server_connection_id':'a'*32,'operation_id':'case-main-0','status':200,'body_bytes':100,'path':'/task/ssr'}
        record={'id':0,'kind':'read','worker':0,'outcome':'completed','dispatch_s':.1,'complete_s':1.,'validation_complete_s':1.002,'steps':[{'status':200,'bytes':100,'path':'/task/ssr','server_connection_id':'a'*32,'outcome':'completed'}],
            'read_evidence':{'snapshot':'1:2:','parameters':{'category':None,'brand':None,'min_price_minor':0,'max_price_minor':None,'available_only':True,'offset':0,'limit':20}}}
        bundle={'operations':[record],'sessions':[{'worker':0,'owner_thread':123,'records':[receipt]}],'phase':{'workers_joined':True,'attempts_per_http_request':1,'start_monotonic_ns':'100000000000','finish_monotonic_ns':'102000000000','workers':1}}
        class Reference:
            def verify_query(self,evidence):return True
        def check(value):return verify_operations(value,[{'id':0,'kind':'read'}],'case-main','inventory',Reference(),{'case-main-0':{'status':200,'path':'/query','snapshot':'1:2:'}},0,set(),{}, {},serial=True)
        self.assertEqual(len(check(bundle)),1)
        for change in ('receipt','cpu','coverage','overlap','parameters'):
            bad=deepcopy(bundle)
            if change=='receipt':bad['sessions'][0]['records'][0]['body_receipt_ns']='101500000000'
            if change=='cpu':bad['sessions'][0]['records'][0]['allowed_cpus']=[1]
            if change=='coverage':bad['operations']=[]
            if change=='overlap':bad['operations'][0]['validation_complete_s']=3
            if change=='parameters':bad['operations'][0]['read_evidence']['parameters']['brand']='wrong'
            with self.assertRaises(ValueError,msg=change):check(bad)

    def test_failed_probes_remain_in_the_browser_denominator_and_integrity_errors_stop(self):
        config={'duration_s':20,'strategy':'ssr','prefix':'case','offsets_s':[3,9,15],'max_dispatch_lag_s':.1};start=100_000_000_000
        probes=[{'id':i,'operation_id':f'case-loaded-{i}','intended_offset_s':offset,'strategy':'ssr','cold_context':True,'dispatch_monotonic_ns':str(start+int(offset*1e9)),
            'finished_monotonic_ns':str(start+int((offset+.1)*1e9)),'actual_offset_s':offset,'outcome':'failed','checks_passed':False,'error_type':'TimeoutError','integrity_error':False,
            'requests':[],'responses':[],'evidence':[],'actions':[]} for i,offset in enumerate(config['offsets_s'])]
        report={'status':'completed','task':'inventory','strategy':'ssr','probes':probes,'start_signal':{'measurement_start_monotonic_ns':str(start),'duration_s':20},
            'probes_finished_monotonic_ns':str(start+19_000_000_000),'release_monotonic_ns':str(start+20_000_000_000)}
        actual=verify_browser(report,config,'inventory',None,{},set(),{},{});self.assertEqual(len(actual['probes']),3);self.assertFalse(any(r['qualified'] for r in actual['probes']))
        report['probes'][0]['integrity_error']=True
        with self.assertRaises(ValueError):verify_browser(report,config,'inventory',None,{},set(),{},{})

    def test_unexecuted_failed_browser_api_and_visible_refresh_failure_remain_misses(self):
        config={'duration_s':20,'strategy':'csr','prefix':'case','offsets_s':[3,9,15],'max_dispatch_lag_s':.1};start=100_000_000_000
        probes=[{'id':i,'operation_id':f'case-loaded-{i}','intended_offset_s':offset,'strategy':'csr','cold_context':True,
            'dispatch_monotonic_ns':str(start+int(offset*1e9)),'finished_monotonic_ns':str(start+int((offset+.1)*1e9)),
            'actual_offset_s':offset,'outcome':'failed','checks_passed':False,'error_type':'TimeoutError','integrity_error':False,
            'requests':[],'responses':[],'evidence':[],'actions':[]} for i,offset in enumerate(config['offsets_s'])]
        identity='case-loaded-0-refresh-0';probes[0].update(error_type='BrowserTaskApplicationFailure',
            requests=[{'path':'/api/task','operation_id':identity}],responses=[{'path':'/api/task','operation_id':identity,'status':503,
                'json':{'ok':False,'error':{'code':'origin_http_failure','upstream_status':503}}}],
            failure_state={'action_error':'Task refresh failed','action_status':'Unable to complete action. Please try again.',
                'app_outcome':'failed','error_text':'Unable to load. Please try again.','primary_disabled':True,'secondary_disabled':True})
        report={'status':'completed','task':'inventory','strategy':'csr','probes':probes,
            'start_signal':{'measurement_start_monotonic_ns':str(start),'duration_s':20},'probes_finished_monotonic_ns':str(start+19_000_000_000),
            'release_monotonic_ns':str(start+20_000_000_000)}
        def check(value,events=None):return verify_browser(value,config,'inventory',None,events or {},set(),{}, {})
        actual=check(report);self.assertEqual(len(actual['probes']),3);self.assertFalse(actual['eligible']);self.assertFalse(any(row['qualified'] for row in actual['probes']))
        for mutation in ('successful_without_native','unrecognized_error','wrong_http_status','active_control','unoffered_response','missing_failed_http'):
            bad=deepcopy(report);row=bad['probes'][0]
            if mutation=='successful_without_native':row['responses'][0].update(status=200,json={'ok':True})
            if mutation=='unrecognized_error':row['responses'][0]['json']['error']['code']='unknown'
            if mutation=='wrong_http_status':row['responses'][0]['json']['error']['upstream_status']=500
            if mutation=='active_control':row['failure_state']['primary_disabled']=False
            if mutation=='unoffered_response':row['requests']=[]
            if mutation=='missing_failed_http':row['responses']=[]
            with self.assertRaises(ValueError,msg=mutation):check(bad)
        with self.assertRaises(ValueError):check(report,{identity:{'status':200,'path':'/query','snapshot':'1:2:'}})


if __name__=='__main__':unittest.main()
