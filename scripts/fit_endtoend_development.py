"""Fit the frozen development recipe and report grouped development diagnostics."""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]; sys.path.insert(0, str(ROOT))
from experiments.study_design.decision import evaluate_block, require, scenarios
from experiments.study_design.learner import fit
from experiments.study_design.selection import select


def sha(raw): return hashlib.sha256(raw).hexdigest()
def dump(path, value): path.write_text(json.dumps(value, sort_keys=True, indent=2) + '\n')


def panel_inputs(rows):
    profiles = defaultdict(dict); panels = defaultdict(dict); applications = {}; catalogs = defaultdict(set)
    names = [s.name for s in scenarios()]; positions = {name: i for i, name in enumerate(names)}
    for row in rows:
        context, config = row['context'], row['configuration']; applications[context] = row['application']; catalogs[row['application']].add(config)
        if config in profiles[context]: require(profiles[context][config] == row['profile'], 'scenario changed permitted profile')
        profiles[context][config] = row['profile']; panels[context].setdefault(config, [None] * len(names))[positions[row['scenario']]] = row['feasible']
    require(all(type(v) is bool for panel in panels.values() for values in panel.values() for v in values), 'incomplete diagnostic panel')
    return dict(profiles), dict(panels), applications, {app: sorted(values) for app, values in catalogs.items()}


def hindsight(panels, applications, catalogs):
    """Separate query/rendering adaptation with perfect loaded-outcome knowledge."""
    names = [s.name for s in scenarios()]; by_app = {}
    for app, catalog in sorted(catalogs.items()):
        contexts = [c for c in panels if applications[c] == app]; joint = query = fixed = 0
        for i in range(len(names)):
            joint += sum(any(panels[c][config][i] for config in catalog) for c in contexts) / len(contexts)
            query += max(sum(any(panels[c][config][i] for config in catalog if config.rsplit('/', 1)[1] == renderer) for c in contexts) / len(contexts) for renderer in ('ssr', 'csr'))
            fixed += max(sum(panels[c][config][i] for c in contexts) / len(contexts) for config in catalog)
        by_app[app] = {key: value / len(names) for key, value in [('joint', joint), ('query_only', query), ('fixed', fixed)]}
    means = {key: sum(value[key] for value in by_app.values()) / len(by_app) for key in ('joint', 'query_only', 'fixed')}
    return {'scores': means, 'application_scores': by_app, 'rendering_adaptation_opportunity': means['joint'] - means['query_only'],
        'query_adaptation_opportunity': means['query_only'] - means['fixed'], 'role': 'Perfect-information development ceiling, never a deployable policy or population effect.'}


def diagnostic(rows, model):
    profiles, panels, applications, catalogs = panel_inputs(rows); selected = select(model, profiles, applications, catalogs)
    result = evaluate_block(panels, selected['choices'], applications, catalogs, selected['scenario_names'])
    return {'evaluation': result, 'joint_abstentions': selected['joint_abstentions'], 'joint_decisions': selected['joint_decisions'],
        'choice_counts': {policy: dict(Counter(action for values in choices.values() for action in values)) for policy, choices in selected['choices'].items()},
        'hindsight_decomposition': hindsight(panels, applications, catalogs)}


def fit_artifact(training, training_raw, registry):
    require(training['version'] == 'development-training-rows-v1' and len(training['attempt_registry']) == 9, 'complete nine-attempt compilation required')
    rows = training['rows']; model = fit(rows, registry, training_inputs_sha256=sha(training_raw))
    folds = []
    for field in ('allocation', 'block'):
        values = sorted({row[field] for row in rows}); require(values == [0, 1, 2], 'all declared grouped diagnostic levels required')
        for value in values:
            train = [r for r in rows if r[field] != value]; test = [r for r in rows if r[field] == value]
            require({r['run_id'] for r in train}.isdisjoint({r['run_id'] for r in test}), 'same deployment entered fitting and its held-out fold')
            raw = json.dumps({'diagnostic_group': field, 'held_level': value, 'rows': train}, sort_keys=True, separators=(',', ':')).encode()
            fitted = fit(train, registry, training_inputs_sha256=sha(raw))
            folds.append({'group': field, 'held_level': value, 'training_runs': sorted({r['run_id'] for r in train}),
                'evaluation_runs': sorted({r['run_id'] for r in test}), 'fit': fitted['diagnostics'], **diagnostic(test, fitted)})
    return model, {'role': 'Development fit and grouped diagnostics only', 'confirmatory_inference': False,
        'collection_days': len({r['collection_date_utc'] for r in rows}), 'collection_dates_utc': sorted({r['collection_date_utc'] for r in rows}),
        'scope': 'Folds hold out whole deployments and either allocation IDs or assignment blocks; their scores are descriptive and are not independent-day confidence intervals.',
        'training_inputs_sha256': sha(training_raw), 'fit': model['diagnostics'], 'fitting_applications': model['fit_applications'],
        'in_sample': diagnostic(rows, model), 'grouped_diagnostics': folds,
        'attempts_retained': len(training['attempt_registry']), 'unusable_attempts': [r['run_id'] for r in training['attempt_registry'] if not r['usable']]}


def main():
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument('--data', type=Path, required=True); parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(); directory = args.data.resolve(); manifest = json.loads((directory / 'analysis-manifest.json').read_bytes())
    for record in manifest['files']:
        path = (directory / record['path']).resolve(); require(path.is_relative_to(directory) and not path.is_symlink(), 'input escapes evidence directory')
        raw = path.read_bytes(); require(len(raw) == record['bytes'] and sha(raw) == record['sha256'], 'compiled development bytes differ')
    raw = (directory / 'training-inputs.json').read_bytes(); training = json.loads(raw)
    for evidence in training['evidence']:
        descriptor = evidence['file']; path = (ROOT / descriptor['path']).resolve(); require(path.is_relative_to(directory), 'evidence escapes compiled development directory')
        value = path.read_bytes(); require(len(value) == descriptor['bytes'] and sha(value) == descriptor['sha256'], 'training evidence bytes differ')
    registry_path = ROOT / 'paper/followup/query-rendering-load/protocol/application_reservation_v1.json'; registry_raw = registry_path.read_bytes()
    model, diagnostics = fit_artifact(training, raw, json.loads(registry_raw)); output = args.output.resolve(); output.mkdir(parents=True, exist_ok=False)
    dump(output / 'model.json', model); dump(output / 'development-diagnostics.json', diagnostics)
    dump(output / 'fit-manifest.json', {'training_inputs_sha256': sha(raw), 'registry_sha256': sha(registry_raw), 'exporter_sha256': sha(Path(__file__).read_bytes()),
        'files': [{'path': p.name, 'bytes': p.stat().st_size, 'sha256': sha(p.read_bytes())} for p in sorted(output.iterdir())]})
    print(json.dumps({'fit': model['diagnostics'], 'hindsight': diagnostics['in_sample']['hindsight_decomposition']}))


if __name__ == '__main__': main()
