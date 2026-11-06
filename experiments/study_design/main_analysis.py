"""Exact policy scores from complete audited application/day panels."""
from datetime import date
from fractions import Fraction
import re

from .calendar_analysis import analyze_calendar,rational
from .decision import require,scenarios
from experiments.heldout_tasks.workload import APPLICATIONS

POLICIES=('joint','query_only','profile_fixed')
CONTRASTS={'joint_minus_query_only':('joint','query_only'),'joint_minus_profile_fixed':('joint','profile_fixed'),'query_only_minus_profile_fixed':('query_only','profile_fixed')}


def component(audited,artifacts,claim_sha):
    """Caller must verify raw release bytes, actual claim and the full paired audit."""
    require(audited['status']=='passed' and audited['smoke'] is False and audited['task'] in APPLICATIONS,'complete main application audit required; qualification is excluded')
    task=audited['task'];app=APPLICATIONS[task];day=audited['collection_date_utc'];sample=artifacts['sample_plan']
    require(day in sample['collection_dates_utc'] and date.fromisoformat(day).isoformat()==day and re.fullmatch('[0-9a-f]{40}',claim_sha),'actual fixed main date/claim required')
    contexts=sorted(r['id'] for r in artifacts['main_contexts']['contexts'] if r['application']==app);catalog=artifacts['main_contexts']['catalogs'][app]
    panels=audited['panels'];choices=audited['choices'];names=[s.name for s in scenarios()]
    require(len(contexts)==8 and set(panels)==set(contexts) and len(audited['cells'])==66,'complete eight-context application panel required')
    require(choices['scenario_names']==names and choices['loaded_evaluation_outcomes_used'] is False and set(choices['choices'])==set(POLICIES),'frozen common-information policy decisions required')
    for context in contexts:
        require(set(panels[context])==set(catalog),'missing physical configuration cannot be dropped')
        for config,values in panels[context].items():
            require(len(values)==27 and all(row['scenario']==names[i] and type(row['feasible']) is bool for i,row in enumerate(values)),'complete ordered finite-feasibility scenarios required')
        for policy in POLICIES:
            selected=choices['choices'][policy];require(set(selected)==set(contexts) and len(selected[context])==27 and all(c in catalog for c in selected[context]),'policy context/action coverage differs')
    denominator=len(contexts)*len(names)
    scores={policy:Fraction(sum(panels[c][choices['choices'][policy][c][i]][i]['feasible'] for c in contexts for i in range(27)),denominator) for policy in POLICIES}
    disagreements=sum(choices['choices']['joint'][c][i]!=choices['choices']['query_only'][c][i] for c in contexts for i in range(27))
    require(abs(scores['joint']-scores['query_only'])<=Fraction(disagreements,denominator),'shared-panel contrast exceeds chosen-action disagreement')
    joint=query=fixed=0
    for i in range(27):
        joint+=sum(any(panels[c][config][i]['feasible'] for config in catalog) for c in contexts)
        query+=max(sum(any(panels[c][config][i]['feasible'] for config in catalog if config.endswith('/'+renderer)) for c in contexts) for renderer in ('ssr','csr'))
        fixed+=max(sum(panels[c][config][i]['feasible'] for c in contexts) for config in catalog)
    hindsight={'joint':Fraction(joint,denominator),'query_only':Fraction(query,denominator),'profile_fixed':Fraction(fixed,denominator)}
    require(hindsight['joint']>=hindsight['query_only']>=hindsight['profile_fixed'],'perfect-information reference ordering differs')
    return {'version':'main-application-day-component-v1','status':'passed','mode':'main','task':task,'application':app,'collection_date_utc':day,
        'source_commit':audited['source_commit'],'run_id':audited['github_run_id'],'release_sha256':audited['release_sha256'],'claim_sha':claim_sha,
        'context_ids':contexts,'scenario_names':names,'finite_comparisons':denominator,'scores':{k:rational(v) for k,v in scores.items()},
        'contrasts':{k:rational(scores[a]-scores[b]) for k,(a,b) in CONTRASTS.items()},'hindsight_reference':{k:rational(v) for k,v in hindsight.items()},
        'primary_action_disagreements':disagreements,'primary_action_disagreement_fraction':rational(Fraction(disagreements,denominator)),
        'observed_shared_action_equality':disagreements==0,
        'hindsight_rendering_flexibility':rational(hindsight['joint']-hindsight['query_only']),
        'hindsight_query_adaptation':rational(hindsight['query_only']-hindsight['profile_fixed']),
        'hindsight_interpretation':'Perfect-information decomposition; not an upper bound on trained-policy gain over a suboptimal trained comparator',
        'model_abstentions':choices['joint_model_abstentions'],'common_fallback_decisions':choices['fallback_decisions'],
        'independent_unit':'One application component of the UTC collection-day aggregate; scenarios and operations are not independent replicates'}


def fraction(row):
    require(set(row)=={'numerator','denominator','value'} and type(row['numerator']) is int and type(row['denominator']) is int and row['denominator']>0,'exact rational component required')
    value=Fraction(row['numerator'],row['denominator']);require(-1<=value<=1 and float(value)==row['value'],'component rational/value differs');return value


def campaign(artifacts,components,source,release_sha):
    sample=artifacts['sample_plan'];dates=sample['collection_dates_utc'];apps=sorted(APPLICATIONS.values());seen={};identities={}
    for row in components:
        require(row['version']=='main-application-day-component-v1' and row['status']=='passed' and row['mode']=='main' and row['application'] in apps
            and row['source_commit']==source and row['release_sha256']==release_sha and row['collection_date_utc'] in dates,'unknown source/date/application component or qualification contamination')
        day=row['collection_date_utc'];key=day,row['application'];require(key not in seen,'duplicate application/date cannot replace an earlier component')
        require(row['finite_comparisons']==216 and row['scenario_names']==[s.name for s in scenarios()] and row['context_ids']==sorted(r['id'] for r in artifacts['main_contexts']['contexts'] if r['application']==row['application']),'fixed component weighting differs')
        require(APPLICATIONS.get(row['task'])==row['application'] and str(row['run_id']).isdigit() and re.fullmatch('[0-9a-f]{40}',row['claim_sha']),'component task/claim identity differs')
        scores={p:fraction(row['scores'][p]) for p in POLICIES}
        require(all(0<=v<=1 and (v*216).denominator==1 for v in scores.values()),'policy score is not on the complete finite grid')
        require(set(row['contrasts'])==set(CONTRASTS) and all(fraction(row['contrasts'][name])==scores[a]-scores[b] for name,(a,b) in CONTRASTS.items()),'policy scores and paired contrasts disagree')
        identity=row['run_id'],row['claim_sha'];require(day not in identities or identities[day]==identity,'application components must share the same claimed four-runner attempt')
        identities[day]=identity;seen[key]=row
    analyses={name:analyze_calendar(dates,apps,{key:fraction(row['contrasts'][name]) for key,row in seen.items()}) for name in CONTRASTS}
    return {'version':'fixed-main-calendar-analysis-v1','source_commit':source,'release_sha256':release_sha,'planned_dates':dates,'applications':apps,
        'audited_application_days':len(seen),'primary_contrast':'joint_minus_query_only','contrasts':analyses,
        'stopping':'fixed_days_no_effect_based_extension','qualification_included':False,'successful_day_renormalization':False}


def day_summary(artifacts,components,day,source,release_sha):
    require(day in artifacts['sample_plan']['collection_dates_utc'] and all(r['collection_date_utc']==day for r in components),'one planned main date required')
    campaign(artifacts,components,source,release_sha)  # Same provenance/grid checks as final analysis.
    apps=sorted(APPLICATIONS.values());missing=len(apps)-len(components);result={}
    for name in CONTRASTS:
        total=sum((fraction(row['contrasts'][name]) for row in components),Fraction())
        result[name]={'lower':rational((total-missing)/len(apps)),'upper':rational((total+missing)/len(apps)),'point_identified':missing==0}
    return {'version':'main-day-summary-v1','collection_date_utc':day,'audited_applications':len(components),'missing_applications':missing,
        'contrasts':result,'meaning':'Equal application weights for this UTC date; unavailable components range over [-1,1]. No interim confidence interval or stopping decision.'}
