from copy import deepcopy
from datetime import date,timedelta
import unittest

from experiments.heldout_tasks.main_design import cells
from experiments.study_design.decision import PROFILE_FEATURES,scenarios
from experiments.study_design.release_preparation import protocols
from experiments.study_design.qualification_budget import AMENDMENT,PRIOR_CLAIMS,validate_budget


class ReleasePreparationContracts(unittest.TestCase):
    def setUp(self):
        start=date(2027,1,1)
        self.calendar={'version':'reserved-calendar-plan-v1','campaign_id':'synthetic-calendar-test','execution_repository':'shravaniparsi/query-rendering-benchmark',
            'collection_dates_utc':[(start+timedelta(days=i)).isoformat() for i in range(60)],'qualification_dates_utc':['2026-12-30'],
            'stopping':'fixed_days_no_effect_based_extension'}
        self.registry={'applications':[{'id':'issue-dashboard','role':'development'},{'id':'inventory-reservation','role':'heldout'},{'id':'time-window-analytics','role':'heldout'}]}
        self.contract={'threshold_family':{'read_deadlines_ms':[50,100,250],'write_deadlines_ms':[50,100,250],'paint_deadlines_ms':[250,500,1000],
            'completion_fraction':.99,'deadline_fraction':.95,'scenario_names':[s.name for s in scenarios()]},'semantics':{},
            'policy':{'abstention_predicted_probability_margin':.05,'features_are_not_yet_available':'legacy fixture'},'development_replay_inputs':{}}

    def build(self):return protocols(self.calendar,self.registry,self.contract,today=date(2026,12,29))

    def test_fixed_protocols_produce_full_matched_panels_and_common_fallback(self):
        value=self.build();self.assertEqual(set(value['profile_protocol']['feature_allowlist']),PROFILE_FEATURES)
        self.assertEqual(set(value['profile_protocol']['performance_profile_fallbacks'].values()),{'maintained/ssr'})
        self.assertEqual(len(value['main_contexts']['contexts']),16);self.assertEqual(value['sample_plan']['day_count'],60)
        for task in ('inventory','analytics'):
            self.assertEqual(len(cells(value,task,'2027-01-01')),66);self.assertEqual(len(cells(value,task,'2026-12-30',True)),42)
        self.assertTrue(value['decision_contract']['main_protocol_ready']);self.assertNotIn('development_replay_inputs',value['decision_contract'])
        self.assertEqual(value['decision_contract']['policy']['abstention_predicted_probability_margin'],.05)

    def test_past_overlapping_repeated_or_effect_extended_calendar_is_rejected(self):
        baseline=deepcopy(self.calendar)
        changes=[{'qualification_dates_utc':['2026-12-28']},{'qualification_dates_utc':['2027-01-01']},
            {'collection_dates_utc':baseline['collection_dates_utc'][:-1]},
            {'collection_dates_utc':[baseline['collection_dates_utc'][0]]*60},{'stopping':'extend_until_significant'},
            {'execution_repository':'other/repo'}]
        for change in changes:
            with self.subTest(change=change):
                self.calendar={**baseline,**change}
                with self.assertRaises(ValueError):self.build()

    def test_reserved_roles_and_deadlines_cannot_change_during_preparation(self):
        self.registry['applications'][1]['role']='development'
        with self.assertRaises(ValueError):self.build()
        self.registry['applications'][1]['role']='heldout';self.contract['threshold_family']['read_deadlines_ms'][0]=51
        with self.assertRaises(ValueError):self.build()

    def test_compact_calendar_has_twenty_dates_full_qualification_and_explicit_precision_goal(self):
        self.calendar.update(version='reserved-calendar-plan-v2',collection_dates_utc=self.calendar['collection_dates_utc'][:20],qualification_dates_utc=['2026-12-30','2026-12-31'])
        value=self.build();self.assertEqual(value['sample_plan']['day_count'],20)
        self.assertEqual(value['sample_plan']['qualification_attempt_limit'],2)
        self.assertEqual(value['decision_contract']['precision']['target_95_interval_half_width'],.05)
        for task in ('inventory','analytics'):self.assertEqual(len(cells(value,task,'2026-12-30',True)),66)
        baseline=deepcopy(self.calendar)
        for change in ({'collection_dates_utc':baseline['collection_dates_utc']+[baseline['collection_dates_utc'][-1]]},{'qualification_dates_utc':['2026-12-29']},
            {'collection_dates_utc':baseline['collection_dates_utc'][:-1]+['2027-01-22']}):
            self.calendar={**baseline,**change}
            with self.subTest(change=change),self.assertRaises(ValueError):self.build()

    def test_approved_amendment_keeps_both_failures_and_all_scientific_protocols(self):
        self.calendar.update(version='reserved-calendar-plan-v2',collection_dates_utc=self.calendar['collection_dates_utc'][:20],qualification_dates_utc=['2026-12-30'])
        original=self.build()
        self.calendar.update(version='reserved-calendar-plan-v3',campaign_id='reserved-main-v2',qualification_amendment=deepcopy(AMENDMENT))
        amended=self.build();sample=amended['sample_plan']
        self.assertEqual(validate_budget(sample),3);self.assertEqual(sample['qualification_budget_dates_utc'],['2026-11-07','2026-11-08','2026-12-30'])
        self.assertEqual(sample['qualification_dates_utc'],['2026-12-30']);self.assertEqual(sample['qualification_amendment']['prior_claims'],PRIOR_CLAIMS)
        for name in ('profile_protocol','main_contexts','decision_contract'):self.assertEqual(original[name],amended[name])
        for task in ('inventory','analytics'):self.assertEqual(len(cells(amended,task,'2026-12-30',True)),66)

    def test_amendment_rejects_renamed_campaign_dropped_history_extra_slots_and_short_buffer(self):
        self.calendar.update(version='reserved-calendar-plan-v3',campaign_id='reserved-main-v2',collection_dates_utc=self.calendar['collection_dates_utc'][:20],
            qualification_dates_utc=['2026-12-30'],qualification_amendment=deepcopy(AMENDMENT))
        baseline=deepcopy(self.calendar)
        for change in ({'campaign_id':'renamed'},{'qualification_dates_utc':['2026-12-30','2026-12-31']},
            {'qualification_dates_utc':['2026-12-31']},{'qualification_amendment':{**deepcopy(AMENDMENT),'total_limit':4}},
            {'qualification_amendment':{**deepcopy(AMENDMENT),'prior_claims':PRIOR_CLAIMS[1:]}}):
            self.calendar={**deepcopy(baseline),**change}
            with self.subTest(change=change),self.assertRaises(ValueError):self.build()

    def test_full_qualification_budget_cannot_be_erased_or_expanded(self):
        sample={'qualification_attempt_limit':3,'campaign_id':'reserved-main-v2','day_count':20,'qualification_date_mode':'complete_profile_and_loaded_full_panel',
            'qualification_dates_utc':['2026-12-30'],'qualification_budget_dates_utc':['2026-11-07','2026-11-08','2026-12-30'],'qualification_amendment':deepcopy(AMENDMENT)}
        self.assertEqual(validate_budget(sample),3)
        for change in ({'qualification_attempt_limit':4},{'qualification_attempt_limit':None},{'qualification_budget_dates_utc':['2026-12-30']},
            {'qualification_amendment':None},{'day_count':21}):
            with self.subTest(change=change),self.assertRaises(ValueError):validate_budget({**sample,**change})


if __name__=='__main__':unittest.main()
