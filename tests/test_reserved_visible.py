from copy import deepcopy
import unittest
from experiments.heldout_tasks.visible import verify_visible,verify_failure,verify_paint


class ReservedVisibleContracts(unittest.TestCase):
    def test_all_three_visible_paint_markers_match_materialized_text(self):
        evidence={'visible_text':{'count':'Products: 1','titles':['Product 1'],'values':['Available: 2']}}
        rows=[{'identifier':key,'text':value,'visible':True} for key,value in [('task-count','Products: 1'),('task-first-title','Product 1'),('task-first-value','Available: 2')]]
        self.assertTrue(verify_paint(evidence,rows))
        with self.assertRaises(ValueError):verify_paint(evidence,rows[:-1])
        rows[-1]['text']='Loading…'
        with self.assertRaises(ValueError):verify_paint(evidence,rows)

    def test_inventory_visible_text_and_facets_reject_dataset_only_agreement(self):
        evidence={'read_operation':'inventory_page','itemsCount':1,'items':[{'id':1,'remaining_stock':2,'price_minor':137}],
            'facets':{'brand':{'B':1},'category':{'C':1}},'visible_text':{'count':'Products: 1','titles':['Product 1'],'values':['Available: 2'],'prices':['Price: $1.37'],
            'facet_labels':{'brand':[{'value':'B','text':'B (1)'}],'category':[{'value':'C','text':'C (1)'}]}}}
        self.assertTrue(verify_visible(evidence))
        for key in ('count','titles','values','prices'):
            bad=deepcopy(evidence);bad['visible_text'][key]='incorrect'
            with self.assertRaises(ValueError):verify_visible(bad)
        bad=deepcopy(evidence);bad['visible_text']['facet_labels']['brand'][0]['text']='B (9)'
        with self.assertRaises(ValueError):verify_visible(bad)

    def test_analytics_signed_exact_currency_window_and_visible_failure(self):
        evidence={'read_operation':'event_time_groups','groupsCount':1,'eventCount':2,'parameters':{'group_by':['region']},'window':{'start_s':99,'end_s':100},
            'groups':[{'region':'R','net_total_minor':-101}],'visible_text':{'count':'Groups: 1 · Events: 2','titles':['R'],'values':['Net sales: -$1.01'],'window':'Time window: 99 inclusive to 100 exclusive'}}
        self.assertTrue(verify_visible(evidence));bad=deepcopy(evidence);bad['visible_text']['values']=['Net sales: $-1.01']
        with self.assertRaises(ValueError):verify_visible(bad)
        fault={'text':'Unable to load. Please try again.','outcome':'failed','error':'origin_http_failure','upstream_status':'503','articles':0,'primary_disabled':True,'secondary_disabled':True}
        self.assertTrue(verify_failure(fault));fault['secondary_disabled']=False
        with self.assertRaises(ValueError):verify_failure(fault)


if __name__=='__main__':unittest.main()
