"""Fixed regularized logistic model of finite-workload feasibility.

Only serial cost features, declared workload conditions, action identity and the
unchanged deadline scenarios enter prediction. Query/application identities are
not one-hot regressors. No reserved application may enter fitting.
"""
from collections import Counter, defaultdict
import hashlib
import json
import math

from .decision import PROFILE_FEATURES, require, scenarios, validate_profile, validate_training_apps

VERSION = 'finite-feasibility-model-v1'
FEATURE_NAMES = ('read_deadline_ratio', 'write_a_deadline_ratio', 'write_b_deadline_ratio', 'paint_deadline_ratio',
    'offered_service_per_db_cpu', 'read_cv', 'payload_kib', 'fixture_rows', 'result_rows',
    'read_fraction', 'write_a_fraction', 'write_b_fraction', 'csr_action', 'logical_http_rate_per_target_cpu', 'pool_size')
HYPERPARAMETERS = {'penalty': .01, 'intercept_penalty': .001, 'max_iterations': 100,
    'gradient_tolerance': 1e-8, 'line_search_steps': 30, 'constant_label_mode': 'Jeffreys-smoothed constant score'}


def canonical_sha(value): return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def vector(profile, configuration, scenario):
    features = validate_profile(profile); require(set(features) == PROFILE_FEATURES, 'complete permitted feature interface required')
    query, renderer = configuration.rsplit('/', 1)
    require(query == profile['query_candidate'] and renderer in ('ssr', 'csr'), 'profile/action identity differs')
    require(all(features[k] > 0 for k in ('target_logical_cpus', 'db_logical_cpus', 'pool_size', 'ssr_request_count', 'csr_request_count')), 'positive resource/request budgets required')
    paint = features[renderer + '_serial_paint_median_ms']; request_count = features[renderer + '_request_count']
    service = sum(features[weight] * features[cost] for weight, cost in (
        ('read_fraction', 'read_service_median_ms'), ('write_a_fraction', 'write_a_service_median_ms'), ('write_b_fraction', 'write_b_service_median_ms')))
    raw = (features['read_service_median_ms'] / scenario.read_ms, features['write_a_service_median_ms'] / scenario.write_ms,
        features['write_b_service_median_ms'] / scenario.write_ms, paint / scenario.paint_ms,
        features['offered_rate'] * service / (1000 * features['db_logical_cpus']), features['read_service_cv'],
        features['read_payload_bytes'] / 1024, features['fixture_rows'], features['result_rows'],
        features['read_fraction'], features['write_a_fraction'], features['write_b_fraction'], float(renderer == 'csr'),
        features['offered_rate'] * (features['read_fraction'] * request_count + features['write_a_fraction'] + features['write_b_fraction']) / features['target_logical_cpus'], features['pool_size'])
    return [value if index in (9, 10, 11, 12) else math.log1p(value) for index, value in enumerate(raw)]


def sigmoid(value):
    if value >= 0: return 1 / (1 + math.exp(-value))
    exp = math.exp(value); return exp / (1 + exp)


def solve(matrix, right):
    rows = [list(values) + [target] for values, target in zip(matrix, right)]; n = len(rows)
    for column in range(n):
        pivot = max(range(column, n), key=lambda index: abs(rows[index][column]))
        require(abs(rows[pivot][column]) > 1e-14, 'regularized Hessian is singular')
        rows[column], rows[pivot] = rows[pivot], rows[column]; scale = rows[column][column]
        rows[column] = [value / scale for value in rows[column]]
        for index in range(n):
            if index == column: continue
            factor = rows[index][column]
            rows[index] = [left - factor * right for left, right in zip(rows[index], rows[column])]
    return [row[-1] for row in rows]


def training_panel(rows, registry):
    require(rows, 'development rows required'); applications = sorted({row['application'] for row in rows}); validate_training_apps(applications, registry)
    scenario_map = {value.name: value for value in scenarios()}; panels = defaultdict(set); catalogs = defaultdict(set); identities = set(); contexts = {}
    vectors = []; labels = []
    for row in rows:
        require(row['feature_phase'] == 'permitted_serial_profile' and row['label_phase'] == 'development_loaded' and type(row['feasible']) is bool, 'loaded features or invalid development label')
        application = row['application']; context = row['context']; configuration = row['configuration']; scenario = row['scenario']
        require(isinstance(context, str) and context and scenario in scenario_map and row['profile']['application'] == application, 'training context/profile/scenario differs')
        key = application, context, configuration, scenario; require(key not in identities, 'duplicate training observation'); identities.add(key)
        require(context not in contexts or contexts[context] == application, 'context spans applications'); contexts[context] = application
        catalogs[application].add(configuration); panels[application, context].add((configuration, scenario))
        vectors.append(vector(row['profile'], configuration, scenario_map[scenario])); labels.append(int(row['feasible']))
    for (application, _), values in panels.items():
        catalog = catalogs[application]; parts = [value.rsplit('/', 1) for value in catalog]; queries = {value[0] for value in parts}
        require(len(queries) >= 2 and catalog == {q + '/' + r for q in queries for r in ('ssr', 'csr')}, 'complete query/renderer catalog required')
        require(values == {(config, scenario) for config in catalog for scenario in scenario_map}, 'incomplete development matched panel')
    per_application = Counter(application for application, _ in panels)
    weights = [1 / (len(applications) * per_application[row['application']] * len(panels[row['application'], row['context']])) for row in rows]
    require(math.isclose(sum(weights), 1, abs_tol=1e-10), 'training weights differ')
    return applications, vectors, labels, weights


def fit(rows, registry, *, training_inputs_sha256):
    require(isinstance(training_inputs_sha256, str) and len(training_inputs_sha256) == 64 and all(c in '0123456789abcdef' for c in training_inputs_sha256), 'actual training artifact identity required')
    applications, raw, labels, weights = training_panel(rows, registry); n_features = len(FEATURE_NAMES)
    means = [sum(weight * values[j] for values, weight in zip(raw, weights)) for j in range(n_features)]
    scales = [math.sqrt(max(0, sum(weight * (values[j] - means[j]) ** 2 for values, weight in zip(raw, weights)))) for j in range(n_features)]
    scales = [value if value > 1e-12 else 1 for value in scales]
    matrix = [[1] + [(value - mean) / scale for value, mean, scale in zip(values, means, scales)] for values in raw]
    parameters = {'feature_names': list(FEATURE_NAMES), 'means': means, 'scales': scales, 'coefficients': None, 'constant_score': None}
    diagnostics = {'rows': len(rows), 'contexts': len({(r['application'], r['context']) for r in rows}), 'positive_labels': sum(labels), 'iterations': 0, 'converged': True}
    if len(set(labels)) == 1:
        parameters['constant_score'] = (sum(labels) + .5) / (len(labels) + 1)
    else:
        size = n_features + 1; beta = [0.] * size; penalties = [HYPERPARAMETERS['intercept_penalty']] + [HYPERPARAMETERS['penalty']] * n_features
        def objective(coefficients):
            total = .5 * sum(penalty * value * value for penalty, value in zip(penalties, coefficients))
            for values, label, weight in zip(matrix, labels, weights):
                z = sum(a * b for a, b in zip(values, coefficients)); total += weight * (max(z, 0) - label * z + math.log1p(math.exp(-abs(z))))
            return total
        diagnostics['converged'] = False
        for iteration in range(HYPERPARAMETERS['max_iterations']):
            gradient = [p * value for p, value in zip(penalties, beta)]; hessian = [[0.] * size for _ in range(size)]
            for j in range(size): hessian[j][j] = penalties[j]
            for values, label, weight in zip(matrix, labels, weights):
                probability = sigmoid(sum(a * b for a, b in zip(values, beta))); residual = weight * (probability - label); curvature = weight * probability * (1 - probability)
                for j in range(size):
                    gradient[j] += residual * values[j]
                    for k in range(size): hessian[j][k] += curvature * values[j] * values[k]
            diagnostics['iterations'] = iteration + 1
            if max(map(abs, gradient)) <= HYPERPARAMETERS['gradient_tolerance']: diagnostics['converged'] = True; break
            step = solve(hessian, gradient); current = objective(beta); directional = sum(a * b for a, b in zip(gradient, step))
            for attempt in range(HYPERPARAMETERS['line_search_steps']):
                factor = .5 ** attempt; candidate = [value - factor * change for value, change in zip(beta, step)]
                if objective(candidate) <= current - 1e-4 * factor * directional: beta = candidate; break
            else: raise ValueError('fixed logistic line search failed')
        require(diagnostics['converged'], 'fixed logistic solver did not converge'); parameters['coefficients'] = beta
        diagnostics['regularized_objective'] = objective(beta)
    return {'version': VERSION, 'fit_applications': applications, 'hyperparameters_frozen': True, 'hyperparameters': dict(HYPERPARAMETERS),
        'feature_allowlist': sorted(PROFILE_FEATURES), 'training_inputs_sha256': training_inputs_sha256,
        'training_rows_sha256': canonical_sha(rows), 'parameters': parameters, 'diagnostics': diagnostics,
        'interpretation': 'Predicted finite-workload feasibility scores; no population reliability or probability-calibration claim.'}


def predict(model, profile, configuration, scenario):
    require(model['version'] == VERSION and model['hyperparameters'] == HYPERPARAMETERS and model['hyperparameters_frozen'] is True, 'model/learner specification differs')
    values = vector(profile, configuration, scenario); parameters = model['parameters']
    require(parameters['feature_names'] == list(FEATURE_NAMES) and len(parameters['means']) == len(parameters['scales']) == len(values) and
        all(type(value) in (float, int) and math.isfinite(value) for value in parameters['means']) and
        all(type(value) in (float, int) and math.isfinite(value) and value > 0 for value in parameters['scales']), 'model scale/interface differs')
    if parameters['constant_score'] is not None:
        require(parameters['coefficients'] is None and type(parameters['constant_score']) in (float, int) and 0 <= parameters['constant_score'] <= 1, 'constant model differs')
        return parameters['constant_score']
    coefficients = parameters['coefficients']; require(isinstance(coefficients, list) and len(coefficients) == len(values) + 1 and all(type(v) in (float, int) and math.isfinite(v) for v in coefficients), 'model coefficients differ')
    transformed = [1] + [(value - mean) / scale for value, mean, scale in zip(values, parameters['means'], parameters['scales'])]
    return sigmoid(sum(a * b for a, b in zip(transformed, coefficients)))
