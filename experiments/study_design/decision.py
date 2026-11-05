"""Finite-workload feasibility, matched policy evaluation and profiling boundaries.

These functions score audited observations. They do not certify population SLOs
or establish that a feature's declared provenance is true.
"""
from dataclasses import dataclass
import itertools
import math

ISSUE_CONFIGS=tuple(f'{q}/{r}' for q,r in itertools.product(('correlated','batched','maintained'),('ssr','csr')))
OUTCOMES={'completed','failed','admission_drop','dispatch_lag_drop','drain_timeout'}
PROFILE_FEATURES={'read_service_median_ms','read_service_cv','write_a_service_median_ms','write_b_service_median_ms',
    'ssr_serial_paint_median_ms','csr_serial_paint_median_ms','read_payload_bytes','read_fraction','write_a_fraction',
    'write_b_fraction','offered_rate','target_logical_cpus','db_logical_cpus','pool_size','result_rows','fixture_rows','ssr_request_count','csr_request_count'}


def require(value,message):
    if not value:raise ValueError(message)


def number(value):return type(value) in (int,float) and math.isfinite(value)


@dataclass(frozen=True)
class Scenario:
    read_ms:float
    write_ms:float
    paint_ms:float
    completion_fraction:float=.99
    deadline_fraction:float=.95
    def __post_init__(self):
        require(all(number(v) and v>0 for v in (self.read_ms,self.write_ms,self.paint_ms)),'positive finite deadlines required')
        require(all(number(v) and 0<v<=1 for v in (self.completion_fraction,self.deadline_fraction)),'fraction bounds differ')
    @property
    def name(self):return f'read-{self.read_ms:g}_write-{self.write_ms:g}_paint-{self.paint_ms:g}'


def scenarios():
    # Engineering sensitivity family, not empirically established user SLOs.
    return tuple(Scenario(r,w,p) for r,w,p in itertools.product((50,100,250),(50,100,250),(250,500,1000)))



def validate_threshold_family(value):
    expected={'read_deadlines_ms':[50,100,250],'write_deadlines_ms':[50,100,250],
              'paint_deadlines_ms':[250,500,1000],'completion_fraction':.99,'deadline_fraction':.95,
              'scenario_names':[s.name for s in scenarios()]}
    require(all(value.get(k)==v for k,v in expected.items()),'threshold contract differs from executed definitions')
    return scenarios()


def score(records,probes,scenario,*,integrity_verified,ambiguous_writes=False):
    require(integrity_verified is True,'audited semantic/clock/resource integrity required')
    require(type(ambiguous_writes) is bool,'ambiguous-write flag must be explicit')
    require(records and probes,'logical work and browser probes required')
    require(len({r['id'] for r in records})==len(records),'duplicate logical identities')
    require(len({r['id'] for r in probes})==len(probes),'duplicate browser identities')
    groups={name:{'offered':0,'completed':0,'within_deadline':0} for name in ('read','write_a','write_b','browser')}
    for row in records:
        require(row['outcome'] in OUTCOMES,'unknown logical outcome')
        require(row['kind'] in ('read','write'),'unknown operation kind')
        if row['kind']=='read':kind='read';deadline=scenario.read_ms
        else:
            require(row['mutation'] in ('comment','state','write_a','write_b'),'unknown mutation mapping')
            kind='write_a' if row['mutation'] in ('comment','write_a') else 'write_b';deadline=scenario.write_ms
        g=groups[kind];g['offered']+=1
        if row['outcome']=='completed':
            times=[row[k] for k in ('intended_s','dispatch_s','complete_s')]
            require(all(number(v) and v>=0 for v in times) and times==sorted(times),'invalid operation clocks')
            g['completed']+=1;g['within_deadline']+=int((row['complete_s']-row['intended_s'])*1000<=deadline)
    for row in probes:
        require(row['outcome'] in ('completed','failed','dispatch_lag_drop'),'unknown browser outcome')
        require(type(row['qualified']) is bool,'browser clock/window qualification required')
        if row['qualified']:require(row['outcome']=='completed','failed browser cannot qualify')
        g=groups['browser'];g['offered']+=1
        if row['outcome']=='completed' and row['qualified']:
            paint=row['critical_content_paint_ms'];require(number(paint) and paint>0,'valid visible paint required')
            g['completed']+=1;g['within_deadline']+=int(paint<=scenario.paint_ms)
    require(all(g['offered'] for g in groups.values()),'read, both write classes and browser must be represented')
    for group in groups.values():
        group['completion_fraction']=group['completed']/group['offered']
        group['deadline_fraction']=group['within_deadline']/group['offered']
        group['feasible']=group['completion_fraction']>=scenario.completion_fraction and group['deadline_fraction']>=scenario.deadline_fraction
    return {'scenario':scenario.name,'feasible':not ambiguous_writes and all(g['feasible'] for g in groups.values()),
            'groups':groups,'ambiguous_writes':ambiguous_writes,'interpretation':'finite scheduled-workload score'}


def validate_profile(value):
    require(set(value)=={'application','query_candidate','phase','source_commit','evidence_sha256','features'},'unexpected profile envelope')
    require(value['phase']=='permitted_serial_profile','loaded or test outcomes cannot be policy features')
    require(isinstance(value['application'],str) and value['application'],'application identity missing')
    candidate=value['query_candidate']
    require(isinstance(candidate,str) and 0<len(candidate)<=80 and all(c.isalnum() or c in '-_' for c in candidate),'query candidate identity missing/invalid')
    for field,length in (('source_commit',40),('evidence_sha256',64)):
        require(isinstance(value[field],str) and len(value[field])==length and all(c in '0123456789abcdef' for c in value[field]),'profile provenance identity differs')
    require(isinstance(value['features'],dict) and value['features'] and set(value['features'])<=PROFILE_FEATURES,'nonpermitted feature')
    require(all(number(v) and v>=0 for v in value['features'].values()),'features must be nonnegative finite numeric scalars')
    for field in ('read_fraction','write_a_fraction','write_b_fraction'):
        if field in value['features']:require(value['features'][field]<=1,'mix fraction exceeds one')
    mix=[value['features'].get(k) for k in ('read_fraction','write_a_fraction','write_b_fraction')]
    if all(v is not None for v in mix):require(math.isclose(sum(mix),1,abs_tol=1e-12),'workload mix differs')
    return dict(value['features'])


def choose(predicted_scores,baseline,margin=.05):
    require(predicted_scores and baseline in predicted_scores,'baseline prediction missing')
    require(number(margin) and 0<=margin<=1,'invalid abstention margin')
    require(all(number(v) and 0<=v<=1 for v in predicted_scores.values()),'predicted scores outside zero/one')
    best=max(predicted_scores.values());ties=sorted(k for k,v in predicted_scores.items() if math.isclose(v,best,abs_tol=1e-12))
    # Retain baseline in exact ties and whenever expected gain is below margin.
    abstain=baseline in ties or best-predicted_scores[baseline]<margin-1e-12
    return {'configuration':baseline if abstain else ties[0],'abstained':abstain,'predicted_gain':best-predicted_scores[baseline]}


def evaluate_block(panel,choices,context_applications,catalogs,scenario_names):
    """Equal application, within-application context, and scenario weights.

    panel[context][configuration] is the full ordered vector of Boolean scenario
    scores. choices[policy][context] gives one configuration per scenario. All configurations are observed; oracle is a hindsight reference.
    """
    require(panel and set(panel)==set(context_applications),'missing or extra context')
    require({'joint','query_only','profile_fixed'}<=set(choices),'strong baselines missing')
    require(set(catalogs)==set(context_applications.values()),'application catalogs differ')
    lengths=set()
    for context,rows in panel.items():
        application=context_applications[context];catalog=catalogs[application]
        require(len(catalog)==len(set(catalog)) and len(catalog)>=4,'distinct query/rendering candidates required')
        parts=[k.rsplit('/',1) for k in catalog]
        require(all(len(p)==2 and p[0] and p[1] in ('ssr','csr') for p in parts),'configuration identities differ')
        queries={p[0] for p in parts}
        require(len(queries)>=2 and set(catalog)=={f'{q}/{r}' for q in queries for r in ('ssr','csr')},'both rendering paths required for every query candidate')
        require(set(rows)==set(catalog),'incomplete matched configuration panel')
        for values in rows.values():
            require(values and all(type(v) is bool for v in values),'scenario scores must be explicit Boolean outcomes')
            lengths.add(len(values))
    require(len(lengths)==1,'scenario vectors differ')
    require(scenario_names and all(isinstance(n,str) and n for n in scenario_names) and len(scenario_names)==len(set(scenario_names)) and next(iter(lengths))==len(scenario_names),'scenario identity/length differs')
    n_scenarios=next(iter(lengths))
    for policy,actions in choices.items():
        require(set(actions)==set(panel),'policy must choose for every context')
        for context,configs in actions.items():
            require(isinstance(configs,list) and len(configs)==n_scenarios,'one policy choice per threshold scenario required')
            require(all(config in panel[context] for config in configs),'policy chose unknown configuration')
    for application in catalogs:
        contexts=[c for c,a in context_applications.items() if a==application]
        for i in range(n_scenarios):
            require(len({choices['profile_fixed'][c][i] for c in contexts})==1,'profile-fixed policy varies across workload contexts')
            require(len({choices['query_only'][c][i].rsplit('/',1)[-1] for c in contexts})==1,'query-only baseline changed rendering across workload contexts')
    n_scenarios=next(iter(lengths));oracle={c:[any(values[i] for values in rows.values()) for i in range(n_scenarios)] for c,rows in panel.items()}
    def aggregate(values):
        by_app={a:sum(sum(values[c])/n_scenarios for c in panel if context_applications[c]==a)/sum(context_applications[c]==a for c in panel) for a in catalogs}
        return {'score':sum(by_app.values())/len(by_app),'application_scores':by_app}
    hindsight=aggregate(oracle);policies={}
    for policy,actions in choices.items():
        result=aggregate({c:[panel[c][config][i] for i,config in enumerate(configs)] for c,configs in actions.items()});result['regret_to_hindsight']=hindsight['score']-result['score'];policies[policy]=result
    return {'policies':policies,'hindsight_reference':hindsight,'primary_joint_minus_query_only':policies['joint']['score']-policies['query_only']['score'],
            'independent_unit':'one collection-day aggregate of matched deployment panels','scenario_names':list(scenario_names),
            'weights':'equal applications, contexts within application, and threshold scenarios','confirmatory_inference':False}


def validate_training_apps(applications,registry):
    roles={r['id']:r['role'] for r in registry['applications']}
    require(len(roles)==len(registry['applications']),'duplicate application registration')
    require(applications and len(applications)==len(set(applications)),'distinct training applications required')
    require(all(roles.get(a)=='development' for a in applications),'held-out or unknown application cannot enter fitting')
    return True


def validate_release(freeze,registry,registry_sha256,contract_sha256):
    fields={'model_source_commit','model_artifact_sha256','profile_protocol_sha256','sample_plan_sha256','main_context_manifest_sha256',
            'registry_sha256','decision_contract_sha256','fit_applications','hyperparameters_frozen'}
    require(set(freeze)==fields,'complete frozen evaluation release required')
    for field in fields-{'fit_applications','hyperparameters_frozen'}:
        value=freeze[field];length=40 if field=='model_source_commit' else 64
        require(isinstance(value,str) and len(value)==length and all(c in '0123456789abcdef' for c in value),'release digest missing/invalid')
    require(freeze['registry_sha256']==registry_sha256 and freeze['decision_contract_sha256']==contract_sha256,'release used a different registry/decision contract')
    require(freeze['hyperparameters_frozen'] is True,'model selection remains open')
    validate_training_apps(freeze['fit_applications'],registry)
    return True
