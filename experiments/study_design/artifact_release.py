"""Bind a reserved-performance release to actual artifact and committed code bytes."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
from datetime import date

from .decision import PROFILE_FEATURES, require, scenarios, validate_threshold_family, validate_training_apps

VERSION = 'reserved-performance-release-v2'
ARTIFACTS = {'model', 'training_inputs', 'profile_protocol', 'sample_plan', 'main_contexts', 'registry', 'decision_contract'}
REQUIRED_SOURCES = {'experiments/study_design/decision.py', 'experiments/study_design/learner.py',
    'experiments/study_design/artifact_release.py', 'experiments/study_design/selection.py'}


def sha256(raw): return hashlib.sha256(raw).hexdigest()


def digest(value, length=64):
    require(isinstance(value, str) and re.fullmatch('[0-9a-f]{' + str(length) + '}', value), 'invalid frozen digest')
    return value


def confined(root, name):
    require(isinstance(name, str) and name and '\\' not in name, 'normalized relative path required')
    path = PurePosixPath(name)
    require(not path.is_absolute() and '..' not in path.parts and str(path) == name, 'artifact path escapes/aliases root')
    current = root
    for part in path.parts:
        current = current / part; require(not current.is_symlink(), 'symlink artifact/source path forbidden')
    require(current.resolve().is_relative_to(root) and current.is_file(), 'artifact/source file missing or outside root')
    return current


def checked_file(root, descriptor):
    require(isinstance(descriptor, dict) and set(descriptor) == {'path', 'sha256', 'bytes'}, 'complete file descriptor required')
    digest(descriptor['sha256']); require(type(descriptor['bytes']) is int and descriptor['bytes'] > 0, 'positive artifact byte length required')
    raw = confined(root, descriptor['path']).read_bytes()
    require(len(raw) == descriptor['bytes'] and sha256(raw) == descriptor['sha256'], 'actual artifact bytes differ: ' + descriptor['path'])
    return raw


def verify_release(root, release_path, *, expected_release_sha256):
    """No source/provenance labels alone can unlock reserved performance.

    The acquisition caller must supply the previously frozen release digest.
    Successful verification returns parsed artifacts; it never starts a run.
    """
    root = Path(root).resolve(); digest(expected_release_sha256)
    raw = confined(root, str(release_path)).read_bytes()
    require(sha256(raw) == expected_release_sha256, 'release envelope bytes differ from frozen identity')
    release = json.loads(raw)
    require(set(release) == {'version', 'model_source_commit', 'artifacts', 'source_files', 'fit_applications', 'hyperparameters_frozen'}, 'complete release envelope required')
    require(release['version'] == VERSION and release['hyperparameters_frozen'] is True, 'wrong/unfrozen release')
    commit = digest(release['model_source_commit'], 40)
    require(isinstance(release['artifacts'], dict) and set(release['artifacts']) == ARTIFACTS, 'actual model/profile/sample/context/registry/contract/training files required')
    paths = [v['path'] for v in release['artifacts'].values()]; require(len(set(paths)) == len(paths), 'artifact paths must be distinct')
    artifacts = {name: json.loads(checked_file(root, value)) for name, value in release['artifacts'].items()}
    sources = release['source_files']; require(isinstance(sources, dict) and REQUIRED_SOURCES <= set(sources), 'frozen decision/learner/gate sources missing')
    for name, expected in sources.items():
        digest(expected); actual = confined(root, name).read_bytes()
        committed = subprocess.run(['git', 'show', f'{commit}:{name}'], cwd=root, check=True, capture_output=True).stdout
        require(sha256(actual) == expected == sha256(committed), 'working or committed model source differs: ' + name)
    registry = artifacts['registry']; validate_training_apps(release['fit_applications'], registry)
    roles = {row['id']: row['role'] for row in registry['applications']}
    model, training = artifacts['model'], artifacts['training_inputs']
    require(model['version'] == 'finite-feasibility-model-v1' and training['version'] == 'development-training-rows-v1', 'model/training artifact version differs')
    require(model['fit_applications'] == release['fit_applications'] and model['hyperparameters_frozen'] is True and
        model['training_inputs_sha256'] == release['artifacts']['training_inputs']['sha256'], 'model training identity differs')
    require(set(model['feature_allowlist']) == PROFILE_FEATURES and len(model['feature_allowlist']) == len(PROFILE_FEATURES), 'model features differ from permitted serial interface')
    rows = training['rows']; require(isinstance(rows, list) and rows, 'actual development training rows required')
    require(model['training_rows_sha256'] == sha256(json.dumps(rows, sort_keys=True, separators=(',', ':')).encode()), 'fitted training rows differ')
    evidence = {}
    for item in training['evidence']:
        require(set(item) == {'file', 'application', 'phase', 'source_commit'} and roles.get(item['application']) == 'development', 'nondevelopment evidence cannot enter fitting')
        require(item['phase'] in ('permitted_serial_profile', 'development_loaded'), 'held-out/unknown evidence phase')
        digest(item['source_commit'], 40); value = json.loads(checked_file(root, item['file'])); key = item['file']['sha256']
        require(key not in evidence and value['application'] == item['application'] and value['phase'] == item['phase'] and value['source_commit'] == item['source_commit'], 'evidence content/provenance differs')
        evidence[key] = item
    for row in rows:
        require(roles.get(row['application']) == 'development' and row['feature_phase'] == 'permitted_serial_profile' and row['label_phase'] == 'development_loaded', 'reserved or loaded feature row cannot enter fitting')
        for field, phase in (('profile_evidence_sha256', 'permitted_serial_profile'), ('label_evidence_sha256', 'development_loaded')):
            item = evidence.get(row[field]); require(item is not None and item['application'] == row['application'] and item['phase'] == phase, 'training row lacks actual permitted evidence')
    require(sorted({row['application'] for row in rows}) == sorted(release['fit_applications']), 'declared and actual training applications differ')
    from .learner import fit, canonical_sha
    reproduced = fit(rows, registry, training_inputs_sha256=release['artifacts']['training_inputs']['sha256'])
    require(canonical_sha(model) == canonical_sha(reproduced), 'frozen model differs from exact replay of the development fit')
    profile = artifacts['profile_protocol']; require(profile['version'] == 'permitted-serial-profile-v2' and profile['phase'] == 'permitted_serial_profile' and
        set(profile['feature_allowlist']) == PROFILE_FEATURES and len(profile['feature_allowlist']) == len(PROFILE_FEATURES) and profile['loaded_outcomes_as_features'] is False, 'profile protocol is incomplete or permits leakage')
    contract = artifacts['decision_contract']; validate_threshold_family(contract['threshold_family'])
    require(contract['main_protocol_ready'] is True and contract['primary_comparator'] == 'query_only' and
        contract['policy']['abstention_predicted_probability_margin'] == .05, 'main decision contract remains open or changes the comparator/margin')
    contexts = artifacts['main_contexts']; require(contexts['version'] == 'main-contexts-v1' and contexts['contexts'], 'fixed main contexts required')
    require(len({row['id'] for row in contexts['contexts']}) == len(contexts['contexts']), 'duplicate main context')
    catalogs = contexts['catalogs']; require(set(catalogs) == {row['application'] for row in contexts['contexts']}, 'main application coverage differs')
    require(set(catalogs) == {application for application, role in roles.items() if role == 'heldout'}, 'all reserved applications must remain in the main evaluation')
    for application, catalog in catalogs.items():
        require(roles.get(application) == 'heldout', 'reserved main performance must use registered held-out applications')
        parts = [value.rsplit('/', 1) for value in catalog]
        require(all(len(value) == 2 and value[0] and value[1] in ('ssr', 'csr') for value in parts), 'main action identities differ')
        queries = {value[0] for value in parts}
        require(len(queries) >= 2 and len(catalog) == len(set(catalog)) and set(catalog) == {q + '/' + r for q in queries for r in ('ssr', 'csr')}, 'main catalogs need both renderers for every query')
    sample = artifacts['sample_plan']; require(sample['version'] == 'main-sample-plan-v1' and sample['performance_phase'] == 'heldout' and
        sample['independent_unit'] == 'UTC collection-day aggregate' and sample['stopping'] == 'fixed_days_no_effect_based_extension', 'main sampling/stopping contract differs')
    days = sample['collection_dates_utc']; require(isinstance(days, list) and 2 <= len(days) <= 60 and days == sorted(set(days)), 'bounded distinct ordered real UTC dates required')
    require(all(isinstance(day, str) and date.fromisoformat(day).isoformat() == day for day in days), 'invalid UTC collection date')
    require(type(sample['day_count']) is int and sample['day_count'] == len(days), 'fixed day count differs')
    for field, artifact in (('model_sha256', 'model'), ('profile_protocol_sha256', 'profile_protocol'), ('main_contexts_sha256', 'main_contexts')):
        require(sample[field] == release['artifacts'][artifact]['sha256'], 'sample plan points to a different artifact: ' + artifact)
    require(sample['scenario_names'] == [value.name for value in scenarios()], 'sample threshold family differs')
    require(sample['daily_context_ids'] == sorted(row['id'] for row in contexts['contexts']) and sample['complete_catalog_panels'] is True and
        sample['retention'] == 'all_attempts_and_offered_operations', 'daily complete panels or retention remain open')
    return {'status': 'passed', 'release_sha256': expected_release_sha256, 'model_source_commit': commit,
        'fit_applications': release['fit_applications'], 'heldout_applications': sorted(catalogs), 'actual_artifacts_checked': len(artifacts),
        'source_files_checked': len(sources), 'training_rows_checked': len(rows), 'evidence_files_checked': len(evidence), 'artifacts': artifacts}
