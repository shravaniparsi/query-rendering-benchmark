from copy import deepcopy
import unittest

from experiments.study_design.decision import evaluate_block, scenarios
from experiments.study_design.learner import fit
from experiments.study_design.selection import select
from test_feasibility_learner import synthetic_rows


class PolicySelectionContracts(unittest.TestCase):
    def fixture(self, constant=None):
        rows, registry = synthetic_rows(constant); model = fit(rows, registry, training_inputs_sha256='3' * 64)
        profiles = {}
        for row in rows: profiles.setdefault(row['context'], {})[row['configuration']] = deepcopy(row['profile'])
        applications = {context: 'dev' for context in profiles}; catalogs = {'dev': sorted(next(iter(profiles.values())))}
        return rows, model, profiles, applications, catalogs

    def test_equal_information_actions_obey_fixed_comparators(self):
        rows, model, profiles, applications, catalogs = self.fixture(); result = select(model, profiles, applications, catalogs)
        panel = {context: {config: [next(row['feasible'] for row in rows if row['context'] == context and row['configuration'] == config and row['scenario'] == scenario.name) for scenario in scenarios()] for config in catalogs['dev']} for context in profiles}
        value = evaluate_block(panel, result['choices'], applications, catalogs, result['scenario_names'])
        self.assertIn('primary_joint_minus_query_only', value); self.assertFalse(result['loaded_evaluation_outcomes_used'])
        for i in range(27):
            self.assertEqual(len({result['choices']['profile_fixed'][context][i] for context in profiles}), 1)
            self.assertEqual(len({result['choices']['query_only'][context][i].rsplit('/', 1)[1] for context in profiles}), 1)

    def test_constant_model_abstains_and_uses_competent_profile_ties(self):
        _, model, profiles, applications, catalogs = self.fixture(True); result = select(model, profiles, applications, catalogs)
        self.assertEqual(result['choices']['joint'], result['choices']['query_only']); self.assertEqual(result['joint_abstentions'], result['joint_decisions'])
        self.assertTrue(all(value == 'fast/ssr' for values in result['choices']['query_only'].values() for value in values))

    def test_new_application_profiles_are_prediction_inputs_not_refitting(self):
        _, model, profiles, applications, catalogs = self.fixture(); before = deepcopy(model)
        for values in profiles.values():
            for profile in values.values(): profile['application'] = 'reserved'
        result = select(model, profiles, dict.fromkeys(profiles, 'reserved'), {'reserved': catalogs['dev']})
        self.assertEqual(model, before); self.assertEqual(len(result['choices']['joint']), 2)
        profiles['low'].pop('fast/ssr')
        with self.assertRaises(ValueError): select(model, profiles, dict.fromkeys(profiles, 'reserved'), {'reserved': catalogs['dev']})


if __name__ == '__main__': unittest.main()
