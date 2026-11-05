import copy
import unittest
import json
from pathlib import Path
from experiments.study_design.decision import Scenario,scenarios,score,choose,evaluate_block,validate_profile,validate_training_apps,validate_release,validate_threshold_family,ISSUE_CONFIGS
from scripts.verify_study_design_report import compare
from experiments.study_design.precision import planning_table,iid_zero_failure_illustration,iid_zero_failure_required


class DecisionContracts(unittest.TestCase):
    def observations(self):
        records=[{'id':i,'kind':'read' if i==0 else 'write','mutation':'comment' if i==1 else 'state',
                  'outcome':'completed','intended_s':0,'dispatch_s':0,'complete_s':.05} for i in range(3)]
        probes=[{'id':0,'outcome':'completed','qualified':True,'critical_content_paint_ms':250}]
        return records,probes
    def test_deadline_boundary_and_offered_denominator(self):
        records,probes=self.observations();s=Scenario(50,50,250)
        self.assertTrue(score(records,probes,s,integrity_verified=True)['feasible'])
        records[0]['complete_s']=.051
        self.assertFalse(score(records,probes,s,integrity_verified=True)['feasible'])
        records[0]['outcome']='failed'
        result=score(records,probes,s,integrity_verified=True)
        self.assertEqual(result['groups']['read']['deadline_fraction'],0);self.assertEqual(result['groups']['read']['offered'],1)
    def test_all_drops_censor_ambiguity_and_late_browser_are_retained_failures(self):
        for outcome in ('failed','admission_drop','dispatch_lag_drop','drain_timeout'):
            records,probes=self.observations();records[1]['outcome']=outcome
            self.assertFalse(score(records,probes,Scenario(50,50,250),integrity_verified=True)['feasible'])
        records,probes=self.observations();probes[0]['qualified']=False
        self.assertFalse(score(records,probes,Scenario(50,50,250),integrity_verified=True)['feasible'])
        records,probes=self.observations();self.assertFalse(score(records,probes,Scenario(50,50,250),integrity_verified=True,ambiguous_writes=True)['feasible'])
    def test_bad_integrity_clocks_unknown_types_and_missing_classes_rejected(self):
        for mode in ('integrity','clock','duplicate','missing','browser','mutation'):
            records,probes=self.observations();verified=True
            if mode=='integrity':verified=False
            elif mode=='clock':records[0]['dispatch_s']=1
            elif mode=='duplicate':records[1]['id']=records[0]['id']
            elif mode=='missing':records.pop()
            elif mode=='browser':probes[0]['critical_content_paint_ms']=float('nan')
            else:records[1]['mutation']='unknown'
            with self.subTest(mode=mode):
                with self.assertRaises(ValueError):score(records,probes,Scenario(50,50,250),integrity_verified=verified)
    def test_scenario_grid_and_bounds(self):
        self.assertEqual(len(scenarios()),27);self.assertEqual(len({s.name for s in scenarios()}),27)
        for bad in (-1,0,float('nan'),True):
            with self.assertRaises(ValueError):Scenario(bad,100,500)
    def test_abstention_and_boundary_do_not_force_a_winner(self):
        self.assertEqual(choose({'a':.5,'b':.54},'a')['configuration'],'a')
        self.assertEqual(choose({'a':.5,'b':.55},'a')['configuration'],'b')
        self.assertEqual(choose({'a':.5,'b':.5},'b')['configuration'],'b')
        for scores in ({'a':float('nan')},{'a':1.1}):
            with self.assertRaises(ValueError):choose(scores,'a')
    def profile(self):
        return {'application':'inventory-reservation','query_candidate':'direct','phase':'permitted_serial_profile','source_commit':'a'*40,'evidence_sha256':'b'*64,
                'features':{'read_service_median_ms':5,'read_fraction':.8,'write_a_fraction':.1,'write_b_fraction':.1}}
    def test_profile_rejects_loaded_labels_nested_payloads_and_wrong_mix(self):
        self.assertEqual(validate_profile(self.profile())['read_service_median_ms'],5)
        for mode in ('phase','feature','nested','mix','hash'):
            p=self.profile()
            if mode=='phase':p['phase']='heldout_loaded_evaluation'
            elif mode=='feature':p['features']['test_p95_ms']=100
            elif mode=='nested':p['features']['read_service_median_ms']={'hidden':100}
            elif mode=='mix':p['features']['read_fraction']=.5
            else:p['evidence_sha256']='x'*64
            with self.assertRaises(ValueError):validate_profile(p)
    def panel(self):
        configs=list(ISSUE_CONFIGS);apps={'a1':'A','a2':'A','b1':'B'}
        panel={c:{k:[False,False] for k in configs} for c in apps}
        for c in panel:panel[c][configs[0]]=[True,False];panel[c][configs[1]]=[False,True]
        choices={p:{c:[configs[0],configs[0]] for c in apps} for p in ('joint','query_only','profile_fixed')}
        return panel,choices,apps,{'A':configs,'B':configs}
    def test_scenario_dependent_choices_oracle_and_equal_application_weight(self):
        panel,choices,apps,catalogs=self.panel()
        # Improve only application B, which has fewer workload contexts.
        choices['joint']['b1'][1]=ISSUE_CONFIGS[1]
        result=evaluate_block(panel,choices,apps,catalogs,["loose","strict"])
        self.assertEqual(result['primary_joint_minus_query_only'],.25)
        self.assertEqual(result['hindsight_reference']['score'],1)
        self.assertEqual(result['policies']['joint']['regret_to_hindsight'],.25)
        self.assertFalse(result['confirmatory_inference'])
    def test_missing_query_and_false_fixed_baselines_rejected(self):
        for mode in ('configuration','context','fixed','query-only','choice','vector'):
            panel,choices,apps,catalogs=self.panel()
            if mode=='configuration':panel['a1'].pop(ISSUE_CONFIGS[-1])
            elif mode=='context':panel.pop('a1')
            elif mode=='fixed':choices['profile_fixed']['a2'][0]=ISSUE_CONFIGS[1]
            elif mode=='query-only':choices['query_only']['a2'][0]=ISSUE_CONFIGS[1]
            elif mode=='choice':choices['joint']['a1'][0]='unknown'
            else:choices['joint']['a1'].pop()
            with self.subTest(mode=mode):
                with self.assertRaises(ValueError):evaluate_block(panel,choices,apps,catalogs,["loose","strict"])
    def test_failure_of_chosen_configuration_counts_against_policy(self):
        panel,choices,apps,catalogs=self.panel();panel['b1'][ISSUE_CONFIGS[0]]=[False,False]
        result=evaluate_block(panel,choices,apps,catalogs,["loose","strict"])
        self.assertEqual(result['policies']['joint']['application_scores']['B'],0)
        self.assertEqual(result['policies']['joint']['score'],.25)
    def test_replay_comparison_rejects_changed_classification_and_counts(self):
        baseline={'feasible':True,'offered':100,'fraction':.95}
        self.assertEqual(compare(baseline,{**baseline,'fraction':.95+1e-14}),['$.fraction'])
        for bad in ({**baseline,'feasible':False},{**baseline,'offered':99},{**baseline,'offered':100.0},{**baseline,'fraction':.951}):
            with self.assertRaises(ValueError):compare(baseline,bad)
    def test_threshold_changes_cannot_silently_use_old_scoring(self):
        root=Path(__file__).resolve().parents[1]
        contract=json.loads((root/'paper/followup/query-rendering-load/protocol/main_decision_components_v1.json').read_text())
        family=contract['threshold_family'];self.assertEqual(len(validate_threshold_family(family)),27)
        for key in ('completion_fraction','deadline_fraction','read_deadlines_ms','scenario_names'):
            bad=copy.deepcopy(family);bad[key]=.8 if key.endswith('fraction') else []
            with self.assertRaises(ValueError):validate_threshold_family(bad)
    def test_holdout_or_unknown_application_cannot_enter_fit_manifest(self):
        registry={'applications':[{'id':'dev','role':'development'},{'id':'held','role':'heldout'}]}
        self.assertTrue(validate_training_apps(['dev'],registry))
        for apps in (['held'],['unknown'],['dev','held'],['dev','dev'],[]):
            with self.assertRaises(ValueError):validate_training_apps(apps,registry)
    def test_release_requires_all_frozen_manifests_and_exact_registry(self):
        registry={'applications':[{'id':'dev','role':'development'},{'id':'held','role':'heldout'}]}
        freeze={k:'a'*64 for k in ('model_artifact_sha256','profile_protocol_sha256','sample_plan_sha256','main_context_manifest_sha256','registry_sha256','decision_contract_sha256')}
        freeze.update(model_source_commit='a'*40,fit_applications=['dev'],hyperparameters_frozen=True)
        self.assertTrue(validate_release(freeze,registry,'a'*64,'a'*64))
        for mode in ('incomplete','profile','heldout','registry','selection'):
            bad=copy.deepcopy(freeze)
            if mode=='incomplete':bad.pop('sample_plan_sha256')
            elif mode=='profile':bad['profile_protocol_sha256']=None
            elif mode=='heldout':bad['fit_applications']=['held']
            elif mode=='registry':bad['registry_sha256']='b'*64
            else:bad['hyperparameters_frozen']=False
            with self.assertRaises(ValueError):validate_release(bad,registry,'a'*64,'a'*64)
    def test_precision_is_scenario_based_and_iid_illustration_is_explicit(self):
        rows=planning_table();self.assertEqual(len(rows),25)
        a=next(r for r in rows if r['independent_day_blocks']==20 and r['assumed_day_contrast_sd']==.2)
        self.assertAlmostEqual(a['conditional_half_width'],2.093*.2/(20**.5))
        self.assertLess(iid_zero_failure_illustration(3)['one_sided_lower_bound'],.37)
        self.assertEqual(iid_zero_failure_required(),299)
        for bad in (0,-1,True):
            with self.assertRaises(ValueError):iid_zero_failure_illustration(bad)


if __name__=='__main__':unittest.main()
