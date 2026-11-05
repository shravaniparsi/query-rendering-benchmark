from copy import deepcopy
import unittest

from experiments.study_design.decision import scenarios
from experiments.study_design.learner import fit, predict, training_panel


def synthetic_rows(constant=None):
    registry = {'applications': [{'id': 'dev', 'role': 'development'}, {'id': 'reserved', 'role': 'heldout'}]}; rows = []
    for context, rate in (('low', 16), ('high', 64)):
        for query, read in (('fast', 2), ('slow', 80)):
            for renderer in ('ssr', 'csr'):
                features = {'read_service_median_ms': read, 'read_service_cv': .2, 'write_a_service_median_ms': 3,
                    'write_b_service_median_ms': 5, 'ssr_serial_paint_median_ms': 100, 'csr_serial_paint_median_ms': 300,
                    'read_payload_bytes': 3000, 'read_fraction': .8, 'write_a_fraction': .1, 'write_b_fraction': .1,
                    'offered_rate': rate, 'target_logical_cpus': 1, 'db_logical_cpus': 1, 'pool_size': 4,
                    'result_rows': 20, 'fixture_rows': 1000, 'ssr_request_count': 1, 'csr_request_count': 2}
                profile = {'application': 'dev', 'query_candidate': query, 'phase': 'permitted_serial_profile',
                    'source_commit': '1' * 40, 'evidence_sha256': '2' * 64, 'features': features}
                for scenario in scenarios():
                    feasible = read * (4 if rate == 64 else 1) <= scenario.read_ms and features[renderer + '_serial_paint_median_ms'] <= scenario.paint_ms
                    rows.append({'application': 'dev', 'context': context, 'configuration': query + '/' + renderer, 'scenario': scenario.name,
                        'profile': deepcopy(profile), 'feasible': feasible if constant is None else constant,
                        'feature_phase': 'permitted_serial_profile', 'label_phase': 'development_loaded'})
    return rows, registry


class FeasibilityLearnerContracts(unittest.TestCase):
    def test_fixed_solver_is_deterministic_bounded_and_fits_physical_toy(self):
        rows, registry = synthetic_rows(); first = fit(rows, registry, training_inputs_sha256='3' * 64); second = fit(rows, registry, training_inputs_sha256='3' * 64)
        self.assertEqual(first, second); self.assertTrue(first['diagnostics']['converged'])
        lookup = {value.name: value for value in scenarios()}; predictions = [predict(first, row['profile'], row['configuration'], lookup[row['scenario']]) for row in rows]
        self.assertTrue(all(0 <= value <= 1 for value in predictions)); self.assertGreater(sum((p >= .5) == row['feasible'] for p, row in zip(predictions, rows)) / len(rows), .9)
        self.assertNotIn('application_identity', first['parameters']['feature_names']); self.assertNotIn('query_identity', first['parameters']['feature_names'])

    def test_constant_labels_cannot_invent_action_differences(self):
        for value in (False, True):
            rows, registry = synthetic_rows(value); model = fit(rows, registry, training_inputs_sha256='3' * 64)
            predictions = [predict(model, row['profile'], row['configuration'], scenarios()[0]) for row in rows]
            self.assertEqual(len(set(predictions)), 1); self.assertIsNone(model['parameters']['coefficients'])

    def test_heldout_fitting_loaded_features_and_incomplete_panels_rejected(self):
        rows, registry = synthetic_rows(); bad = deepcopy(rows); bad[0]['application'] = 'reserved'; bad[0]['profile']['application'] = 'reserved'
        with self.assertRaises(ValueError): fit(bad, registry, training_inputs_sha256='3' * 64)
        bad = deepcopy(rows); bad[0]['profile']['phase'] = 'development_loaded'
        with self.assertRaises(ValueError): fit(bad, registry, training_inputs_sha256='3' * 64)
        with self.assertRaises(ValueError): fit(rows[:-1], registry, training_inputs_sha256='3' * 64)
        with self.assertRaises(ValueError): fit(rows + [rows[0]], registry, training_inputs_sha256='3' * 64)

    def test_equal_context_weights_and_corrupt_model_rejected(self):
        rows, registry = synthetic_rows(); _, _, _, weights = training_panel(rows, registry)
        self.assertAlmostEqual(sum(w for r, w in zip(rows, weights) if r['context'] == 'low'), .5)
        self.assertAlmostEqual(sum(w for r, w in zip(rows, weights) if r['context'] == 'high'), .5)
        model = fit(rows, registry, training_inputs_sha256='3' * 64); model['parameters']['coefficients'][0] = float('nan')
        with self.assertRaises(ValueError): predict(model, rows[0]['profile'], rows[0]['configuration'], scenarios()[0])
        bad = deepcopy(rows[0]['profile']); bad['features']['pool_size'] = 0
        with self.assertRaises(ValueError): predict(fit(rows, registry, training_inputs_sha256='3' * 64), bad, rows[0]['configuration'], scenarios()[0])


if __name__ == '__main__': unittest.main()
