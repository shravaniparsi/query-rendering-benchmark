"""Prospective protocol files for the unchanged model and reserved main design."""
from copy import deepcopy
from datetime import date,timedelta
import itertools
import re

from .decision import PROFILE_FEATURES,require,scenarios,validate_threshold_family
from experiments.heldout_tasks.workload import APPLICATIONS,VERSION as TRACE_VERSION


def calendar_plan(value,today):
    require(set(value)=={'version','campaign_id','execution_repository','collection_dates_utc','qualification_dates_utc','stopping'},'complete explicit calendar input required')
    require(value['version'] in ('reserved-calendar-plan-v1','reserved-calendar-plan-v2') and value['execution_repository']=='shravaniparsi/query-rendering-benchmark'
        and re.fullmatch('[a-z0-9-]{1,40}',value['campaign_id']) and value['stopping']=='fixed_days_no_effect_based_extension','calendar identity/stopping differs')
    main=value['collection_dates_utc'];qual=value['qualification_dates_utc']
    compact=value['version']=='reserved-calendar-plan-v2';n=20 if compact else 60
    require(isinstance(main,list) and len(main)==n and isinstance(qual,list) and 1<=len(qual)<=(2 if compact else 4),'fixed main-date count and bounded qualification attempts required')
    for days in (main,qual):
        require(all(isinstance(day,str) and date.fromisoformat(day).isoformat()==day for day in days) and days==sorted(set(days)),'ordered distinct real UTC dates required')
    require(min(qual)>=today.isoformat() and min(main)>max(qual),'qualification must be prospective and finish before the main calendar')
    if compact:
        first=date.fromisoformat(main[0]);require(main==[(first+timedelta(days=i)).isoformat() for i in range(20)],'compact main dates must be consecutive')
        require(set(qual)<={(first-timedelta(days=i)).isoformat() for i in (1,2)},'compact qualification uses only the two pre-main dates')
    return deepcopy(value)


def protocols(calendar,registry,contract,*,today):
    plan=calendar_plan(calendar,today);compact=plan['version']=='reserved-calendar-plan-v2';n=20 if compact else 60
    value=deepcopy(contract);validate_threshold_family(value['threshold_family'])
    roles={row['id']:row['role'] for row in registry['applications']}
    require(len(roles)==len(registry['applications']) and {a for a,r in roles.items() if r=='heldout'}==set(APPLICATIONS.values())
        and roles.get('issue-dashboard')=='development','unchanged reserved/development application roles required')
    contexts=[];catalogs={}
    for task,application in APPLICATIONS.items():
        catalogs[application]=[q+'/'+r for q,r in itertools.product(('correlated','batched','maintained'),('ssr','csr'))]
        for rows,rate,mix in itertools.product((1000,100000),(16,64),(.02,.5)):
            contexts.append({'id':f'{task}-{rows}-{rate}-{mix}','application':application,'rows':rows,'offered_rate':rate,'nominal_write_fraction':mix,'duration_s':20})
    main_contexts={'version':'main-contexts-v1','contexts':contexts,'catalogs':catalogs,
        'background_mix_scope':'Background logical offers only; separately retain all browser initial/refresh reads and both business mutations',
        'deployment_boundary':'Query/storage design is selected for a workload block; setup/build costs are reported separately'}
    profile={'version':'permitted-serial-profile-v2','phase':'permitted_serial_profile','feature_allowlist':sorted(PROFILE_FEATURES),
        'loaded_outcomes_as_features':False,'workload_trace_version':TRACE_VERSION,
        'serial':{'operations_per_renderer':60,'read_operations_per_renderer':36,'write_a_operations_per_renderer':12,'write_b_operations_per_renderer':12,
            'quiet_probes_per_renderer':3,'quiet_window_s':10,'quiet_probe_offsets_s':[.5,3.5,6.5],
            'logical_pacing':'serial completion-driven; intended, dispatch, body-receipt and validation clocks retained'},
        'overhead':{'rows':1000,'window_s':5,'pairs':3,'workers':4,'max_operations_per_window':50000,'minimum_context_median_ratio':.90,
            'scope':'Marginal CPU sampling cost; shared receipt/hash/semantic costs remain in both modes'},
        'performance_profile_fallbacks':{a:'maintained/ssr' for a in APPLICATIONS.values()},
        'fallback_scope':'Any valid performance-unusable serial profile invokes the same fallback for every policy and all eight application/date contexts; integrity failures stop scoring',
        'selection_commitment':'Complete all twelve serial cells and commit profile/choice hashes on both hosts before the first loaded cell',
        'hardware':{'target_application_gateway_overlay_cpus':1,'target_postgres_cpus':1,'observer_generator_sampler_overlay_cpus':1,'observer_browser_cpus':1,'pool_size':4},
        'profile_cost':'Retain profiling wall time, native transaction service, pool wait and setup/build/storage costs separately'}
    value.update(version='main-decision-contract-v1',status='frozen prospective main contract; acquisition still requires the actual release and planned-date checks',main_protocol_ready=True)
    value['semantics']['browser_endpoint']='Content-matched critical initial paint plus trusted reservation/cancellation or sale/refund completion; three landmarks and a four-second whole-task cap'
    value['policy']['joint']='Use the unchanged development-trained feasibility model; joint may retain the informed query-only action under the frozen 0.05 predicted-score margin'
    value['policy'].pop('features_are_not_yet_available',None)
    value['policy']['final_holdout_catalogs']=deepcopy(catalogs)
    value['precision']={'independent_unit':'UTC collection-day aggregate','main_n_selected':True,'planning_only':False,'fixed_days':n,
        'basis':'Maximum of the predeclared bounded precision planning grid; one-day development supplies no between-day variance estimate',
        'stopping':'fixed_days_no_effect_based_extension','missing_components':'Keep original calendar/application weights and report identification bounds; no replacement or successful-day renormalization',
        'zero_variance':'No point-width population t interval; separately labelled bounded independent-day sensitivities remain available'}
    value.pop('development_replay_inputs',None)
    if compact:
        value['precision'].update(basis='User-accepted fixed twenty-date budget; no measured between-day development variance or guaranteed power',
            target_95_interval_half_width=.05,material_gain_threshold=.05,margin_units='absolute standardized feasibility coverage; distinct from model prediction abstention',
            conditional_planning={'assumed_day_sd':.10,'degrees_of_freedom':19,'t95_rounded':2.093,'half_width':2.093*.10/(20**.5)},
            inconclusive='Report insufficient precision or unsupported assumptions without extending collection',
            population_claims='Conditional paired-day t interval only with complete dates and positive variance under its stated assumptions; bounded independent-day sensitivities reported separately')
    sample={'version':'main-sample-plan-v1','campaign_id':plan['campaign_id'],'execution_repository':plan['execution_repository'],'performance_phase':'heldout',
        'independent_unit':'UTC collection-day aggregate','stopping':plan['stopping'],'collection_dates_utc':plan['collection_dates_utc'],'day_count':n,
        'qualification_dates_utc':plan['qualification_dates_utc'],'qualification_date_mode':'complete_profile_and_loaded_full_panel' if compact else 'complete_profile_and_loaded_smoke',
        'qualification_excluded_from_main':True,'scenario_names':[s.name for s in scenarios()],'daily_context_ids':sorted(r['id'] for r in contexts),
        'complete_catalog_panels':True,'retention':'all_attempts_and_offered_operations','replacement_attempts':False,
        'physical_runner_roles_per_attempt':4,'loaded_background_operations_per_complete_day':76800,'loaded_browser_tasks_per_complete_day':288,
        'serial_operations_per_complete_day':1440,'quiet_browser_tasks_per_complete_day':72,
        'browser_foreground_scope':'Additional initial/refresh reads and business mutations are retained separately from nominal background rate/mix',
        'additional_spending_limit':0,'main_enable_condition':'Independent paired qualification and verified free calendar accounting; no main enablement by this preparation step'}
    if compact:
        first=date.fromisoformat(plan['collection_dates_utc'][0]);sample.update(qualification_attempt_limit=2,
            qualification_budget_dates_utc=sorted((first-timedelta(days=i)).isoformat() for i in (1,2)),full_qualification_cases_per_application=66)
    return {'profile_protocol':profile,'main_contexts':main_contexts,'decision_contract':value,'sample_plan':sample}
