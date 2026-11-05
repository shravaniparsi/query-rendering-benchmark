from copy import deepcopy
import unittest
from experiments.study_design.learner import fit
from experiments.study_design.selection import select
from experiments.study_design.main_selection import select_daily
from test_feasibility_learner import synthetic_rows


class MainSelectionContracts(unittest.TestCase):
    def fixture(self):
        rows,registry=synthetic_rows();model=fit(rows,registry,training_inputs_sha256='3'*64);profiles={}
        for row in rows:profiles.setdefault(row['context'],{})[row['configuration']]=row['profile']
        applications={c:'dev' for c in profiles};catalogs={'dev':sorted(next(iter(profiles.values())))};status={'dev':{'integrity_verified':True,'usable':True,'performance_failure_reasons':[]}}
        return model,profiles,applications,catalogs,status,{'dev':'fast/ssr'}

    def test_complete_profiles_preserve_the_frozen_policy_choices(self):
        model,profiles,applications,catalogs,status,defaults=self.fixture();actual=select_daily(model,profiles,applications,catalogs,status,defaults);expected=select(model,profiles,applications,catalogs)
        self.assertEqual(actual['choices'],expected['choices']);self.assertEqual(actual['joint_model_abstentions'],expected['joint_abstentions']);self.assertEqual(actual['fallback_decisions'],0)

    def test_performance_failure_retains_all_contexts_with_the_same_predetermined_actions(self):
        model,profiles,applications,catalogs,status,defaults=self.fixture();status['dev'].update(usable=False,performance_failure_reasons=['logical_completion'])
        result=select_daily(model,{},applications,catalogs,status,defaults)
        for policy in result['choices'].values():
            self.assertEqual(set(policy),set(applications));self.assertTrue(all(actions==['fast/ssr']*27 for actions in policy.values()))
        self.assertEqual(result['fallback_decisions'],54);self.assertEqual(result['joint_model_abstentions'],0);self.assertTrue(all(v is None for v in result['predicted_scores'].values()))

    def test_corrupt_integrity_and_undeclared_failures_cannot_be_hidden_as_fallbacks(self):
        args=list(self.fixture());args[4]['dev'].update(integrity_verified=False,usable=False,performance_failure_reasons=['logical_completion'])
        with self.assertRaises(ValueError):select_daily(*args)
        args=list(self.fixture());args[4]['dev'].update(usable=False,performance_failure_reasons=['source_mismatch'])
        with self.assertRaises(ValueError):select_daily(*args)
        args=list(self.fixture());args[1].pop('low')
        with self.assertRaises(ValueError):select_daily(*args)


if __name__=='__main__':unittest.main()
