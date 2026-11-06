from copy import deepcopy
from fractions import Fraction
import unittest

from experiments.study_design.decision import scenarios
from experiments.study_design.main_analysis import component,campaign,day_summary
from experiments.heldout_tasks.workload import APPLICATIONS


class MainAnalysisContracts(unittest.TestCase):
    def setUp(self):
        self.names=[s.name for s in scenarios()];contexts=[];catalogs={}
        for task,app in APPLICATIONS.items():
            catalogs[app]=[q+'/'+r for q in ('correlated','batched','maintained') for r in ('ssr','csr')]
            contexts.extend({'id':task+'-'+str(i),'application':app} for i in range(8))
        self.artifacts={'main_contexts':{'contexts':contexts,'catalogs':catalogs},'sample_plan':{'collection_dates_utc':['2027-01-01','2027-01-02']}}

    def audited(self,task='inventory'):
        app=APPLICATIONS[task];catalog=self.artifacts['main_contexts']['catalogs'][app];panels={};choices={p:{} for p in ('joint','query_only','profile_fixed')}
        for i in range(8):
            context=task+'-'+str(i);panels[context]={config:[{'scenario':name,'feasible':(config=='maintained/ssr' and i<4) or (config=='batched/csr' and i<6) or (config=='correlated/ssr' and i>=6)} for name in self.names] for config in catalog}
            for policy in choices:choices[policy][context]=['batched/csr' if policy=='joint' else 'maintained/ssr']*27
        return {'status':'passed','smoke':False,'task':task,'collection_date_utc':'2027-01-01','source_commit':'1'*40,'github_run_id':'123','release_sha256':'2'*64,'cells':[{}]*66,
            'panels':panels,'choices':{'scenario_names':self.names,'choices':choices,'loaded_evaluation_outcomes_used':False,'joint_model_abstentions':0,'fallback_decisions':0}}

    def test_exact_policy_accounting_and_global_renderer_hindsight(self):
        value=component(self.audited(),self.artifacts,'3'*40)
        self.assertEqual(value['scores']['joint']['value'],.75);self.assertEqual(value['scores']['query_only']['value'],.5)
        self.assertEqual(value['contrasts']['joint_minus_query_only']['value'],.25)
        self.assertEqual(value['hindsight_reference']['joint']['value'],1.);self.assertEqual(value['hindsight_reference']['query_only']['value'],.75)
        self.assertEqual(value['hindsight_rendering_flexibility']['value'],.25);self.assertEqual(value['finite_comparisons'],216)

    def test_qualification_or_an_unselected_missing_configuration_cannot_enter_main(self):
        value=self.audited();value['smoke']=True
        with self.assertRaises(ValueError):component(value,self.artifacts,'3'*40)
        value=self.audited();del value['panels']['inventory-7']['correlated/csr']
        with self.assertRaises(ValueError):component(value,self.artifacts,'3'*40)
        value=self.audited();value['panels']['inventory-0']['maintained/ssr'][0]['scenario']='wrong'
        with self.assertRaises(ValueError):component(value,self.artifacts,'3'*40)

    def test_missing_calendar_components_keep_weight_and_duplicates_or_mixed_attempts_fail(self):
        a=component(self.audited(),self.artifacts,'3'*40);b=component(self.audited('analytics'),self.artifacts,'3'*40)
        result=campaign(self.artifacts,[a,b],'1'*40,'2'*64);interval=result['contrasts']['joint_minus_query_only']['finite_campaign_identification']
        self.assertEqual(Fraction(interval['lower']['numerator'],interval['lower']['denominator']),Fraction(-3,8))
        self.assertEqual(Fraction(interval['upper']['numerator'],interval['upper']['denominator']),Fraction(5,8))
        with self.assertRaises(ValueError):campaign(self.artifacts,[a,a],'1'*40,'2'*64)
        bad=deepcopy(b);bad['run_id']='999'
        with self.assertRaises(ValueError):campaign(self.artifacts,[a,bad],'1'*40,'2'*64)
        bad=deepcopy(a);bad['mode']='qualification'
        with self.assertRaises(ValueError):campaign(self.artifacts,[bad],'1'*40,'2'*64)

    def test_changed_contrast_cannot_disagree_with_finite_policy_scores(self):
        value=component(self.audited(),self.artifacts,'3'*40);value['contrasts']['joint_minus_query_only']={'numerator':1,'denominator':2,'value':.5}
        with self.assertRaises(ValueError):campaign(self.artifacts,[value],'1'*40,'2'*64)

    def test_daily_missing_application_keeps_half_the_weight(self):
        value=component(self.audited(),self.artifacts,'3'*40)
        result=day_summary(self.artifacts,[value],'2027-01-01','1'*40,'2'*64)
        self.assertEqual(result['missing_applications'],1)
        self.assertEqual(result['contrasts']['joint_minus_query_only']['lower']['value'],-.375)
        self.assertEqual(result['contrasts']['joint_minus_query_only']['upper']['value'],.625)


if __name__=='__main__':unittest.main()
