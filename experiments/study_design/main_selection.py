"""Common-information daily policies with a declared performance-profile fallback."""
from .decision import require,scenarios
from .selection import select

PERFORMANCE_PROFILE_FAILURES={'logical_completion','quiet_browser_completion','ambiguous_profile_write'}


def select_daily(model,profiles,applications,catalogs,profile_status,defaults):
    """Integrity errors stop acquisition; a performance-unusable profile does not drop a task.

    An application's complete common budget is consumed before this decision.
    All policies use its same predetermined fallback if any permitted serial
    profile is performance-unusable. Loaded outcomes never enter this function.
    """
    apps=set(applications.values());require(applications and set(catalogs)==set(profile_status)==set(defaults)==apps,'complete daily application definitions required')
    require(set(profiles)<=set(applications),'unknown profile context');names=[s.name for s in scenarios()];policies={p:{} for p in ('joint','query_only','profile_fixed')}
    modes={};scores={};model_abstentions=fallback_decisions=0
    for app in sorted(apps):
        status=profile_status[app];require(set(status)=={'integrity_verified','usable','performance_failure_reasons'} and status['integrity_verified'] is True and type(status['usable']) is bool,'verified serial-profile integrity required')
        reasons=status['performance_failure_reasons'];require(isinstance(reasons,list) and len(set(reasons))==len(reasons) and set(reasons)<=PERFORMANCE_PROFILE_FAILURES,'integrity/unknown errors cannot be a performance fallback')
        require(status['usable']==(not reasons),'explicit usable/performance-failure status required');require(defaults[app] in catalogs[app],'registered fallback configuration absent from catalog')
        contexts=sorted(c for c,a in applications.items() if a==app)
        if status['usable']:
            require(all(c in profiles for c in contexts),'usable serial panel missing');value=select(model,{c:profiles[c] for c in contexts},{c:app for c in contexts},{app:catalogs[app]})
            for policy in policies:policies[policy].update(value['choices'][policy])
            scores.update(value['predicted_scores']);model_abstentions+=value['joint_abstentions'];modes[app]='permitted_profiles'
        else:
            for policy in policies:
                for context in contexts:policies[policy][context]=[defaults[app]]*len(names)
            for context in contexts:scores[context]=None
            fallback_decisions+=len(contexts)*len(names);modes[app]='common_performance_profile_fallback'
    return {'choices':policies,'scenario_names':names,'predicted_scores':scores,'selection_modes':modes,'profile_status':profile_status,
        'joint_model_abstentions':model_abstentions,'fallback_decisions':fallback_decisions,'joint_decisions':len(applications)*len(names),
        'loaded_evaluation_outcomes_used':False,'defaults':defaults}
