from copy import deepcopy
from datetime import date,timedelta
import unittest

from experiments.heldout_tasks.main_design import cells
from experiments.study_design.decision import PROFILE_FEATURES,scenarios
from experiments.study_design.release_preparation import protocols


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


if __name__=='__main__':unittest.main()
