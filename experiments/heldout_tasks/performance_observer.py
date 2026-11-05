"""Fixed-budget common profiles, committed choices and full matched action panels."""
import gzip
from collections import Counter
import json
import os
from pathlib import Path
import statistics
import sys
import time

from .inventory import require
from .main_design import cells,trace_for,qualification_order
from .performance_evidence import audit_cell,derive_profile,digest
from .transport import TaskClient
from .workload import APPLICATIONS
from experiments.study_design.main_selection import select_daily
from experiments.issue_dashboard.cpu_budget import bind_current,bind_overlay,overlay_pid,tree
from experiments.issue_dashboard.endtoend_transport import ReceiptSession
from experiments.issue_dashboard.loaded_browser import LoadedBrowser
from experiments.issue_dashboard.offhost_engineering import RouteWatch,request_json,save,verify_routes
from experiments.issue_dashboard.offhost_resources import BudgetWatch
from experiments.issue_dashboard.owned_arrivals import execute_owned,execute_serial_owned,execute_closed_owned
from experiments.issue_dashboard.endtoend_observer import path_controls
from scripts.offhost_connectivity import network_state,check_pair


def compact(path,value):
    with gzip.GzipFile(filename=str(path),mode='wb',mtime=0) as stream:stream.write((json.dumps(value,sort_keys=True,separators=(',',':'))+'\n').encode())


def client_factory(peer,case,suffix,cpus):
    return lambda _:TaskClient(ReceiptSession(peer,8765,persistent=True,expected_cpus=cpus),case['task'],case['strategy'],case['name']+'-'+suffix)


def probe(base,directory,case,cpu,permission,quiet=False):
    directory.mkdir();spec={'duration_s':10 if quiet else 20,'loaded_browser_offsets_s':[.5,3.5,6.5] if quiet else [3,9,15],
        'loaded_browser_deadline_ms':4000,'loaded_browser_max_dispatch_lag_s':.1}
    instance=LoadedBrowser(base,directory,case,cpu,spec,script=Path(__file__).with_name('performance_browser.cjs'))
    instance.config.update(case=case,permission={'root':str(permission.root),'release_path':permission.release_path,'expected_sha256':permission.expected_sha256},
        python_executable=sys.executable,requests_path=str(directory/'request-offers.jsonl'))
    return instance


def daily_choices(artifacts,task,source,serial):
    app=APPLICATIONS[task];contexts=[r for r in artifacts['main_contexts']['contexts'] if r['application']==app];catalog=artifacts['main_contexts']['catalogs'][app]
    require(len(serial)==12 and {(v['case']['rows'],v['case']['candidate'],v['case']['strategy']) for v in serial}==
        {(n,q,r) for n in (1000,100000) for q in ('correlated','batched','maintained') for r in ('ssr','csr')},'all twelve serial cells required before selection')
    derived={};reasons=set()
    for rows in (1000,100000):
        for query in ('correlated','batched','maintained'):
            selected={v['case']['strategy']:v for v in serial if v['case']['rows']==rows and v['case']['candidate']==query}
            value=derive_profile(task,query,rows,source,selected);derived[str(rows)+'/'+query]=value;reasons.update(value['performance_failure_reasons'])
    profiles={}
    if not reasons:
        for context in contexts:
            writes=round(context['offered_rate']*20*context['nominal_write_fraction']);total=context['offered_rate']*20
            known={'offered_rate':context['offered_rate'],'read_fraction':(total-writes)/total,'write_a_fraction':(writes//2)/total,'write_b_fraction':(writes-writes//2)/total}
            profiles[context['id']]={}
            for config in catalog:
                query=config.rsplit('/',1)[0];envelope=derived[str(context['rows'])+'/'+query]['profile'];profiles[context['id']][config]={**envelope,'features':{**envelope['features'],**known}}
    status={app:{'integrity_verified':True,'usable':not reasons,'performance_failure_reasons':sorted(reasons)}}
    defaults=artifacts['profile_protocol']['performance_profile_fallbacks'];require(defaults[app]=='maintained/ssr','prospective common fallback changed')
    choices=select_daily(artifacts['model'],profiles,{r['id']:app for r in contexts},{app:catalog},status,{app:defaults[app]})
    return {'derived':derived,'profiles':profiles,'profile_status':status,'phase':'permitted_serial_profile','loaded_evaluation_outcomes_used':False},choices


def run_observer(output,own,peer_name,peer_ip,task,day,smoke,permission,collected):
    artifacts=permission.artifacts;values=cells(artifacts,task,day,smoke);cpus=sorted(own['allowed_cpus'])[:2]
    require(len(cpus)==2,'two selected observer CPUs required');bind_current(cpus[:1]);overlay=overlay_pid();bind_overlay(overlay,cpus[:1]);base=f'http://{peer_ip}:8765'
    deadline=time.monotonic()+180
    while True:
        try:remote=request_json(base,'/manifest',timeout=3);break
        except OSError:
            if time.monotonic()>deadline:raise
            time.sleep(1)
    check_pair(own,remote);request_json(base,'/qrl/hello',own);serial=[];qualifications=[];committed=False
    def capture(browser=None):
        result={'generator_sampler':tree(os.getpid(),cpus[:1],False),'overlay':tree(overlay,cpus[:1],False)}
        if browser:result['loaded_browser']=browser.sample()
        return result
    for case in values:
        if case['phase']=='loaded' and not committed:
            profiles,choices=daily_choices(artifacts,task,own['source_commit'],serial);save(output/'daily-profiles.json',profiles);save(output/'daily-choices.json',choices)
            commitment={'choices_sha256':digest(choices),'profiles_sha256':digest(profiles),'release_sha256':permission.expected_sha256,'collection_date_utc':day}
            save(output/'policy-commitment-offer.json',{**commitment,'observer_monotonic_ns':str(time.perf_counter_ns()),'loaded_cases_started':0})
            reply=request_json(base,'/qrl/commit-policy',commitment);require(all(reply[k]==v for k,v in commitment.items()),'target policy commitment differs');save(output/'policy-commitment.json',reply);committed=True
        directory=output/case['name'];directory.mkdir();browser=None;budget=None;bundles=[];browser_evidence=None
        require(request_json(base,'/qrl/begin',{'case':case['name'],'observer':own},timeout=120)=={'case':case,'ready':True},'begin differs')
        network_state(own['overlay_name'],peer_name,timeout=30,require_direct=True);routes=RouteWatch(own['overlay_name'],peer_name).start()
        try:
            warm=[{'id':i,'kind':'read'} for i in range(4)];records,native,phase=execute_serial_owned(warm,client_factory(peer_ip,case,'warm',cpus[:1]));value={'operations':records,'sessions':native,'phase':phase}
            compact(directory/'warmup.json.gz',value);bundles.append({'bundle':value,'plan':warm,'prefix':case['name']+'-warm','cpu':cpus[0],'serial':True})
            path_controls(peer_ip,directory,case,'pre',own,smoke,cpus[:1])
            if case['phase']=='qualification':
                pairs=[]
                for pair in range(3):
                    counts={}
                    for enabled in qualification_order(day,int(case['name'].split('-')[1]),pair):
                        label=f'pair-{pair}-'+('on' if enabled else 'off');request_json(base,'/qrl/window-begin',{'case':case['name'],'label':label,'instrumented':enabled})
                        budget=BudgetWatch(capture).start() if enabled else None
                        try:records,native,phase=execute_closed_owned(client_factory(peer_ip,case,label,cpus[:1]),seconds=5,workers=4,max_operations=50000)
                        finally:
                            if budget:save(directory/(label+'-budget.json'),budget.finish());budget=None
                        request_json(base,'/qrl/window-end',{'case':case['name'],'label':label});value={'operations':records,'sessions':native,'phase':phase};compact(directory/(label+'.json.gz'),value)
                        bundles.append({'bundle':value,'prefix':case['name']+'-'+label,'cpu':cpus[0],'closed':True})
                        require(records and all(r['outcome']=='completed' for r in records) and not phase['errors'] and not phase['operation_cap_reached'],'qualification incomplete/capped');counts[enabled]=sum(r['complete_s']<=5 for r in records)
                    require(counts[False]>0 and counts[True]>0,'empty qualification window');pairs.append({'pair':pair,'enabled':counts[True],'disabled':counts[False],'ratio':counts[True]/counts[False]})
                summary={'case':case['name'],'pairs':pairs,'median_ratio':statistics.median(r['ratio'] for r in pairs)};save(directory/'qualification.json',summary);qualifications.append(summary)
                require(summary['median_ratio']>=.90,'marginal CPU sampling observation cost failed qualification')
            else:
                require(len(qualifications)==6,'all six configurations must qualify before serial/load');plan=trace_for(artifacts,case);save(directory/'schedule.json',plan)
                browser=probe(base,directory/('quiet' if case['phase']=='serial' else 'loaded'),case,cpus[1],permission,quiet=case['phase']=='serial');browser.launch()
                request_json(base,'/qrl/window-begin',{'case':case['name'],'label':'main','instrumented':True});budget=BudgetWatch(lambda:capture(browser)).start()
                try:
                    if case['phase']=='loaded':
                        records,native,phase=execute_owned(plan,client_factory(peer_ip,case,'main',cpus[:1]),workers=16,max_dispatch_lag=.05,drain_timeout=30,start_callback=browser.start)
                        left=20-(time.perf_counter_ns()-int(phase['start_monotonic_ns']))/1e9
                        if left>0:time.sleep(left)
                        browser.wait_completed()
                    else:
                        records,native,phase=execute_serial_owned(plan,client_factory(peer_ip,case,'main',cpus[:1]));started=time.perf_counter();browser.start(started);browser.wait_completed();left=10-(time.perf_counter()-started)
                        if left>0:time.sleep(left)
                    value={'operations':records,'sessions':native,'phase':phase};compact(directory/'operations.json.gz',value)
                finally:save(directory/'main-budget.json',budget.finish());budget=None
                request_json(base,'/qrl/window-end',{'case':case['name'],'label':'main'},timeout=120);report=browser.release()
                bundles.append({'bundle':value,'plan':plan,'prefix':case['name']+'-main','cpu':cpus[0],'serial':case['phase']=='serial'});browser_evidence={'report':report,'config':browser.config}
                summary={'offered':len(records),'outcomes':dict(Counter(r['outcome'] for r in records))}
            path_controls(peer_ip,directory,case,'post',own,smoke,cpus[:1])
        finally:
            if budget:save(directory/'interrupted-budget.json',budget.finish())
            if browser:browser.close()
            samples=routes.finish();save(directory/'routes.json',samples)
        verify_routes(samples);reply=request_json(base,'/qrl/end',{'case':case['name']},timeout=120);require(reply['closed'] is True,'end differs');packet=reply['packet'];save(directory/'profile-packet.json',packet)
        audit=audit_cell(case,packet,bundles,browser_evidence);save(directory/'business-audit.json',audit)
        if case['phase']=='serial':serial.append({'case':case,'packet':packet,'bundle':value,'browser_report':report,'audit':audit})
        collected.append({**case,'summary':summary,'business_audit_sha256':digest(audit)});print(json.dumps({'phase':'cell_complete',**case,'summary':summary}),flush=True)
    request_json(base,'/qrl/finish',{'observer':own});save(output/'qualification.json',qualifications);return remote
