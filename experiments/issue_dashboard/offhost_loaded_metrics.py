"""Same-host browser timing integrity and allocation-level feasibility."""
import math
import statistics
from .offhost_engineering import require
from .arrivals import IssueHTML
from .offhost_loaded_design import ROLE,design,cases_for


def check_clock(value):
    before,node,after=(int(value[k]) for k in ('before_ns','node_ns','after_ns'))
    require(value['status']=='passed' and value['monotonic'] is True and value['adjustable'] is False,'clock properties differ')
    require(0<before<=node<=after and (after-before)<=50_000_000,'clock bracket differs')
    require(value['bracket_width_ms']==(after-before)/1e6 and 0<value['resolution_s']<=1e-6,'clock arithmetic differs')


def check_loaded(report,ready,completed,config,case,spec,oracle):
    from scripts.audit_offhost_resources import check_browser
    require(report['status']=='completed' and report['browser'] and ready['contexts']==completed['probes']==3,'loaded protocol incomplete')
    require(config['strategy']==case['strategy'] and config['prefix']==case['name'] and config['duration_s']==spec['duration_s'] and
            config['offsets_s']==spec['loaded_browser_offsets_s'] and config['deadline_ms']==spec['loaded_browser_deadline_ms'] and
            config['max_dispatch_lag_s']==spec['loaded_browser_max_dispatch_lag_s'],'loaded configuration differs')
    signal=report['start_signal'];start=int(signal['measurement_start_monotonic_ns']);end=start+int(spec['duration_s']*1e9)
    require(signal['duration_s']==spec['duration_s'] and 0<int(ready['monotonic_ns'])<=start,'loaded start clock differs')
    finish=int(report['probes_finished_monotonic_ns']);release=int(report['release_monotonic_ns'])
    require(start<=finish<=int(completed['monotonic_ns'])<=release and release>=end and len(report['probes'])==3,'loaded release clocks differ')
    rows=[];snapshots=0
    for i,(raw,offset) in enumerate(zip(report['probes'],spec['loaded_browser_offsets_s'])):
        require(raw['id']==i and raw['operation_id']==f'{case["name"]}-loaded-{i}' and raw['intended_offset_s']==offset and
                raw['strategy']==case['strategy'] and raw['cold_context'] is True and not raw['expected_failure'],'loaded identities differ')
        dispatch=int(raw['dispatch_monotonic_ns']);done=int(raw['finished_monotonic_ns']);actual=(dispatch-start)/1e9
        require(start<=dispatch<=done<=finish and actual>=offset and math.isclose(actual,raw['actual_offset_s'],abs_tol=1e-9),'loaded timing arithmetic differs')
        require(raw['outcome'] in ('completed','failed','dispatch_lag_drop'),'unknown loaded outcome')
        on_time=actual-offset<=spec['loaded_browser_max_dispatch_lag_s'] and done<=end
        if raw['outcome']=='dispatch_lag_drop':require(actual-offset>spec['loaded_browser_max_dispatch_lag_s'] and not raw['checks_passed'],'false browser dispatch drop')
        if raw.get('error_type')=='AssertionError':raise RuntimeError('browser semantic assertion failed')
        if 'evidence' in raw:oracle.verify(raw['evidence']);snapshots+=1
        if raw['outcome']=='completed':
            check_browser({'probes':[raw]},case,'loaded')
            require(raw['document_status']==200 and not raw['page_errors'] and not raw['request_failures'] and not raw['http_errors'],'loaded health differs')
            parser=IssueHTML();parser.feed(raw['document_html']);require(len(parser.main)==1,'loaded document main differs')
            if case['strategy']=='ssr':
                main=parser.main[0];evidence=raw['evidence']
                require(main['data-qrl-outcome']=='completed' and main['data-snapshot']==evidence['snapshot'] and
                        parser.rows==evidence['issues'] and int(parser.count)==evidence['issuesCount'],'SSR document differs from rendered evidence')
            else:require(parser.main[0]['data-qrl-outcome']=='loading' and not parser.rows,'CSR document contract differs')
            interaction=raw['interaction'];click=interaction['click'];response=interaction['dom_commit_ms']-click['start_ms']
            require(click['trusted'] is True and all(math.isfinite(x) and x>=0 for x in (click['start_ms'],click['capture_ms'],interaction['dom_commit_ms'],response)) and
                    click['start_ms']<=click['capture_ms']<=interaction['dom_commit_ms'] and response==interaction['dom_response_ms'] and
                    interaction['final_watch_count']==0 and interaction['final_pressed'] is False,'loaded trusted-click DOM timing differs')
            require(raw['critical_content_paint_ms']<=(done-dispatch)/1e6 and interaction['dom_commit_ms']<=(done-dispatch)/1e6,'page timings exceed probe interval')
            require(len(raw['navigation'])==1 and all(math.isfinite(v) and v>=0 for v in raw['navigation'][0].values()),'navigation evidence differs')
        else:require(raw['checks_passed'] is False and raw.get('error_type'),'failed browser outcome missing')
        rows.append({k:raw[k] for k in ('id','operation_id','intended_offset_s','actual_offset_s','outcome')}|
                    {'on_time':on_time,'qualified':raw['outcome']=='completed' and on_time,
                     'critical_content_paint_ms':raw.get('critical_content_paint_ms'),'dom_response_ms':raw.get('interaction',{}).get('dom_response_ms'),
                     'error_type':raw.get('error_type'),'error':raw.get('error')})
    successes=[r for r in rows if r['outcome']=='completed']
    require(('screenshot_probe_id' in report)==bool(successes),'loaded screenshot evidence differs')
    if successes:require(report['screenshot_probe_id']==successes[0]['id'],'loaded screenshot identity differs')
    qualified=all(r['qualified'] for r in rows)
    descriptive={key:({'median':statistics.median(r[key] for r in rows),'min':min(r[key] for r in rows),'max':max(r[key] for r in rows)} if qualified else None)
                 for key in ('critical_content_paint_ms','dom_response_ms')}
    return {'probes':rows,'eligible':qualified,'snapshots':snapshots,'completed':len(successes),'descriptive':descriptive}


def analyze(audits):
    # Preserve the fixed calibration prefix algorithm with this stage's matrix.
    import itertools
    if len(audits)!=3 or {r['allocation'] for r in audits}!={0,1,2}:raise ValueError('three paired allocations required')
    if len({r['run_id'] for r in audits})!=3 or len({r['source_commit'] for r in audits})!=1:raise ValueError('distinct runs at same source required')
    spec=design();ordered=sorted(audits,key=lambda r:r['allocation']);points=[];indexed=[]
    for audit in ordered:
        if audit['status']!='passed' or audit['role']!=ROLE or audit['smoke'] or audit['design']!=spec:raise ValueError('complete full loaded-browser cohort required')
        planned=cases_for(audit['allocation'])
        if len(audit['cells'])!=len(planned) or any(any(c.get(k)!=v for k,v in p.items()) for c,p in zip(audit['cells'],planned)):raise ValueError('loaded cells differ')
        for c in audit['cells']:
            if c['eligible']!=(c['http_eligible'] and c['browser_summary']['eligible']):raise ValueError('joint qualification differs')
        indexed.append({(c['strategy'],c['placement'],c['rate']):c for c in audit['cells']})
        points.extend({'allocation':audit['allocation'],**cell} for cell in audit['cells'])
    def reasons(cells):
        value=[]
        if any(c['summary']['generator_limited'] for c in cells):value.append('generator_limited')
        if any(c['summary']['censored'] for c in cells):value.append('censored')
        if any(c['ambiguous_writes'] for c in cells):value.append('ambiguous_writes')
        if any(any(s['outcomes'].get('completed',0)/s['offered']<spec['completion_screen'] for s in
                   (c['read_summary'],c['write_summary'],*c['mutation_summaries'].values())) for c in cells):value.append('completion_screen_failed')
        if any(not c['browser_summary']['eligible'] for c in cells):value.append('browser_failed_or_late')
        return value
    def prefix(rows):
        passed=[];first=None
        for row in rows:
            if first is None and row['all_allocations_qualified']:passed.append(row['rate'])
            elif first is None:first=row['rate']
        return {'passing_prefix_rates':passed,'highest_passing_prefix_rate':passed[-1] if passed else None,'first_failed_rung':first,
                'nonmonotone_recovery':first is not None and any(r['rate']>first and r['all_allocations_qualified'] for r in rows),'capacity_identified':False}
    groups=[]
    for strategy,placement in itertools.product(spec['strategies'],spec['placements']):
        rows=[]
        for rate in spec['rates']:
            cells=[index[strategy,placement,rate] for index in indexed]
            rows.append({'rate':rate,'qualified_allocations':[i for i,c in enumerate(cells) if c['eligible']],
                         'all_allocations_qualified':all(c['eligible'] for c in cells),'failure_reasons':reasons(cells)})
        groups.append({'strategy':strategy,'placement':placement,'rungs':rows,**prefix(rows)})
    common=[{'rate':rate,'all_allocations_qualified':all(r['all_allocations_qualified'] for g in groups for r in g['rungs'] if r['rate']==rate),
             'failure_reasons':sorted({reason for g in groups for r in g['rungs'] if r['rate']==rate for reason in r['failure_reasons']})} for rate in spec['rates']]
    return {'role':ROLE,'confirmatory_data':False,'n_allocations':3,'analysis_unit':'paired VM allocation','source_commit':ordered[0]['source_commit'],
            'design':spec,'points':points,'groups':groups,'common_rungs':common,'common_prefix':prefix(common),'main_load_selected':False,
            'scope':'Three sparse cold-context browser probes per twenty-second logical-load window. Descriptive within-allocation medians and ranges only; no browser p95, INP, population or pooled-probe inference. Feasibility is not maximum capacity.'}
