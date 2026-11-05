from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from experiments.study_design.artifact_release import verify_release, REQUIRED_SOURCES, VERSION
from experiments.study_design.decision import PROFILE_FEATURES, scenarios
from experiments.study_design.learner import fit
from test_feasibility_learner import synthetic_rows


class ActualArtifactReleaseContracts(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='qrl-release-fixture-'); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        self.sources = {}
        actual_root = Path(__file__).resolve().parents[1]
        for name in REQUIRED_SOURCES:
            path = self.root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes((actual_root / name).read_bytes())
            self.sources[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        subprocess.run(['git', 'add', '.'], cwd=self.root, check=True)
        subprocess.run(['git', '-c', 'user.name=Release Test', '-c', 'user.email=unit-test@example.invalid', 'commit', '-qm', 'Synthetic gate fixture'], cwd=self.root, check=True)
        self.commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=self.root, text=True).strip()
        self.evidence = []
        for phase in ('permitted_serial_profile', 'development_loaded'):
            value = {'application': 'dev', 'phase': phase, 'source_commit': self.commit}
            self.evidence.append({'file': self.file(phase + '.json', value), **value})
        self.rows, _ = synthetic_rows(True)
        for row in self.rows:
            row['profile'].update(source_commit=self.commit)
            row.update(profile_evidence_sha256=self.evidence[0]['file']['sha256'], label_evidence_sha256=self.evidence[1]['file']['sha256'])
        self.baseline_rows = deepcopy(self.rows)
        threshold = {'read_deadlines_ms': [50, 100, 250], 'write_deadlines_ms': [50, 100, 250], 'paint_deadlines_ms': [250, 500, 1000],
            'completion_fraction': .99, 'deadline_fraction': .95, 'scenario_names': [value.name for value in scenarios()]}
        self.values = {
            'registry': {'applications': [{'id': 'dev', 'role': 'development'}, {'id': 'reserved', 'role': 'heldout'}]},
            'training_inputs': {'version': 'development-training-rows-v1', 'rows': self.rows, 'evidence': self.evidence},
            'model': {'version': 'finite-feasibility-model-v1', 'fit_applications': ['dev'], 'hyperparameters_frozen': True, 'feature_allowlist': sorted(PROFILE_FEATURES)},
            'profile_protocol': {'version': 'permitted-serial-profile-v2', 'phase': 'permitted_serial_profile', 'feature_allowlist': sorted(PROFILE_FEATURES), 'loaded_outcomes_as_features': False},
            'decision_contract': {'threshold_family': threshold, 'main_protocol_ready': True, 'primary_comparator': 'query_only', 'policy': {'abstention_predicted_probability_margin': .05}},
            'main_contexts': {'version': 'main-contexts-v1', 'contexts': [{'id': 'reserved-context', 'application': 'reserved'}],
                'catalogs': {'reserved': ['q1/ssr', 'q1/csr', 'q2/ssr', 'q2/csr']}},
            'sample_plan': {'version': 'main-sample-plan-v1', 'performance_phase': 'heldout', 'independent_unit': 'UTC collection-day aggregate',
                'stopping': 'fixed_days_no_effect_based_extension', 'collection_dates_utc': ['2026-12-01', '2026-12-02'], 'day_count': 2,
                'scenario_names': [value.name for value in scenarios()], 'daily_context_ids': ['reserved-context'], 'complete_catalog_panels': True,
                'retention': 'all_attempts_and_offered_operations'}}
        self.baseline_registry = deepcopy(self.values['registry'])

    def file(self, name, value):
        raw = (json.dumps(value, sort_keys=True, indent=2) + '\n').encode(); (self.root / name).write_bytes(raw)
        return {'path': name, 'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}

    def freeze(self):
        values = deepcopy(self.values); descriptors = {}
        for name in ('registry', 'training_inputs', 'profile_protocol', 'decision_contract', 'main_contexts'): descriptors[name] = self.file(name + '.json', values[name])
        model = fit(self.baseline_rows, self.baseline_registry, training_inputs_sha256=descriptors['training_inputs']['sha256'])
        model.update(values['model']); values['model'] = model
        values['model']['training_rows_sha256'] = hashlib.sha256(json.dumps(values['training_inputs']['rows'], sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        descriptors['model'] = self.file('model.json', values['model'])
        for field, name in (('model_sha256', 'model'), ('profile_protocol_sha256', 'profile_protocol'), ('main_contexts_sha256', 'main_contexts')): values['sample_plan'][field] = descriptors[name]['sha256']
        descriptors['sample_plan'] = self.file('sample_plan.json', values['sample_plan'])
        release = {'version': VERSION, 'model_source_commit': self.commit, 'artifacts': descriptors, 'source_files': self.sources,
            'fit_applications': values['model']['fit_applications'], 'hyperparameters_frozen': True}
        descriptor = self.file('release.json', release); return descriptor['sha256']

    def verify(self, digest): return verify_release(self.root, 'release.json', expected_release_sha256=digest)

    def test_actual_files_and_committed_sources_required(self):
        result = self.verify(self.freeze()); self.assertEqual(result['actual_artifacts_checked'], 7); self.assertEqual(result['training_rows_checked'], len(self.rows))
        (self.root / 'model.json').write_text('{}\n')
        with self.assertRaises(ValueError): self.verify(hashlib.sha256((self.root / 'release.json').read_bytes()).hexdigest())

    def test_envelope_tampering_and_working_source_change_rejected(self):
        digest = self.freeze(); original = (self.root / 'release.json').read_bytes(); (self.root / 'release.json').write_bytes(original + b' ')
        with self.assertRaises(ValueError): self.verify(digest)
        (self.root / 'release.json').write_bytes(original); path = self.root / next(iter(REQUIRED_SOURCES)); path.write_text('# changed\n')
        with self.assertRaises(ValueError): self.verify(digest)

    def test_reserved_training_or_loaded_features_rejected_even_when_rehashed(self):
        self.values['model']['fit_applications'] = ['reserved']
        with self.assertRaises(ValueError): self.verify(self.freeze())
        self.values['model']['fit_applications'] = ['dev']; self.values['training_inputs']['rows'][0]['feature_phase'] = 'development_loaded'
        with self.assertRaises(ValueError): self.verify(self.freeze())

    def test_missing_evidence_and_wrong_context_binding_rejected(self):
        self.values['training_inputs']['rows'][0]['profile_evidence_sha256'] = '0' * 64
        with self.assertRaises(ValueError): self.verify(self.freeze())
        self.values['training_inputs']['rows'][0]['profile_evidence_sha256'] = self.evidence[0]['file']['sha256']; self.values['sample_plan']['daily_context_ids'] = []
        with self.assertRaises(ValueError): self.verify(self.freeze())

    def test_single_day_incomplete_catalog_and_changed_deadlines_rejected(self):
        self.values['sample_plan']['collection_dates_utc'] = ['2026-12-01']; self.values['sample_plan']['day_count'] = 1
        with self.assertRaises(ValueError): self.verify(self.freeze())
        self.values['sample_plan']['collection_dates_utc'].append('2026-12-02'); self.values['sample_plan']['day_count'] = 2
        self.values['main_contexts']['catalogs']['reserved'].pop()
        with self.assertRaises(ValueError): self.verify(self.freeze())
        self.values['main_contexts']['catalogs']['reserved'].append('q2/csr'); self.values['decision_contract']['threshold_family']['read_deadlines_ms'][0] = 51
        with self.assertRaises(ValueError): self.verify(self.freeze())

    def test_symlink_and_parent_escape_paths_rejected(self):
        digest = self.freeze(); path = self.root / 'profile_protocol.json'; content = path.read_bytes(); path.unlink()
        target = self.root / 'alias.json'; target.write_bytes(content); path.symlink_to(target)
        with self.assertRaises(ValueError): self.verify(digest)
        path.unlink(); path.write_bytes(content)
        value = json.loads((self.root / 'release.json').read_text()); value['artifacts']['profile_protocol']['path'] = '../escape.json'
        descriptor = self.file('release.json', value)
        with self.assertRaises(ValueError): self.verify(descriptor['sha256'])

    def test_consistently_rehashed_model_must_still_reproduce_the_fit(self):
        baseline = fit(self.baseline_rows, self.baseline_registry, training_inputs_sha256='3' * 64)
        self.values['model']['parameters'] = deepcopy(baseline['parameters']); self.values['model']['parameters']['constant_score'] = .5
        with self.assertRaises(ValueError): self.verify(self.freeze())


if __name__ == '__main__': unittest.main()
