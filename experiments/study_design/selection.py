"""Equal-information joint, informed query-only, and profile-fixed decisions."""
import math
from .decision import choose, require, scenarios, validate_profile
from .learner import predict


def profile_cost(profile, configuration, scenario):
    value = validate_profile(profile); renderer = configuration.rsplit('/', 1)[1]
    cost = (value['read_fraction'] * value['read_service_median_ms'] / scenario.read_ms +
        value['write_a_fraction'] * value['write_a_service_median_ms'] / scenario.write_ms +
        value['write_b_fraction'] * value['write_b_service_median_ms'] / scenario.write_ms +
        value[renderer + '_serial_paint_median_ms'] / scenario.paint_ms)
    return cost, value[renderer + '_request_count']


def prefer(scores, costs):
    best = max(scores.values()); ties = [key for key, value in scores.items() if math.isclose(value, best, rel_tol=0, abs_tol=1e-12)]
    return min(ties, key=lambda key: (*costs[key], key))


def select(model, profiles, applications, catalogs):
    """Every policy receives the same profile panel and known workload contexts.

    Rendering for query-only and the complete profile-fixed action are selected
    across the declared contexts. Neither may use loaded evaluation outcomes.
    """
    require(profiles and set(profiles) == set(applications) and set(catalogs) == set(applications.values()), 'complete profile/application coverage required')
    contexts = sorted(profiles); thresholds = scenarios(); policies = {key: {context: [] for context in contexts} for key in ('joint', 'query_only', 'profile_fixed')}
    predictions = {}; costs = {}
    for context in contexts:
        application = applications[context]; catalog = catalogs[application]
        require(set(profiles[context]) == set(catalog) and len(catalog) == len(set(catalog)), 'incomplete profile catalog')
        parts = [value.rsplit('/', 1) for value in catalog]; queries = {value[0] for value in parts}
        require(len(queries) >= 2 and all(len(value) == 2 and value[1] in ('ssr', 'csr') for value in parts) and
            set(catalog) == {q + '/' + r for q in queries for r in ('ssr', 'csr')}, 'query/renderer action coverage differs')
        require(all(value['application'] == application for value in profiles[context].values()), 'profile application identity differs')
        predictions[context] = [{config: predict(model, profiles[context][config], config, scenario) for config in catalog} for scenario in thresholds]
        costs[context] = [{config: profile_cost(profiles[context][config], config, scenario) for config in catalog} for scenario in thresholds]
    abstentions = 0
    for application in sorted(catalogs):
        selected = [context for context in contexts if applications[context] == application]; catalog = catalogs[application]
        for index, _ in enumerate(thresholds):
            mean_scores = {config: sum(predictions[c][index][config] for c in selected) / len(selected) for config in catalog}
            mean_costs = {config: tuple(sum(costs[c][index][config][j] for c in selected) / len(selected) for j in (0, 1)) for config in catalog}
            fixed = prefer(mean_scores, mean_costs); query_actions = {}; renderer_scores = {}; renderer_costs = {}
            for renderer in ('ssr', 'csr'):
                actions = {c: prefer({key: value for key, value in predictions[c][index].items() if key.rsplit('/', 1)[1] == renderer}, costs[c][index]) for c in selected}
                query_actions[renderer] = actions
                renderer_scores[renderer] = sum(predictions[c][index][actions[c]] for c in selected) / len(selected)
                renderer_costs[renderer] = tuple(sum(costs[c][index][actions[c]][j] for c in selected) / len(selected) for j in (0, 1))
            renderer = prefer(renderer_scores, renderer_costs)
            for context in selected:
                baseline = query_actions[renderer][context]; choice = choose(predictions[context][index], baseline, margin=.05)
                joint = baseline if choice['abstained'] else prefer(predictions[context][index], costs[context][index])
                policies['profile_fixed'][context].append(fixed); policies['query_only'][context].append(baseline); policies['joint'][context].append(joint)
                abstentions += choice['abstained']
    return {'choices': policies, 'scenario_names': [value.name for value in thresholds], 'predicted_scores': predictions,
        'joint_abstentions': abstentions, 'joint_decisions': len(contexts) * len(thresholds),
        'tie_rule': 'Lower serial service/deadline plus paint/deadline cost, then fewer logical requests, then action identity.',
        'loaded_evaluation_outcomes_used': False}
