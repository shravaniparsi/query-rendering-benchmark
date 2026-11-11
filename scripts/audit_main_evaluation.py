"""Independent full-panel replay of a frozen paired reserved evaluation attempt."""
import argparse
from collections import defaultdict
from datetime import datetime,timezone
import gzip
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from experiments.heldout_tasks.inventory import require
from experiments.heldout_tasks.main_design import cells,trace_for,qualification_order
from experiments.heldout_tasks.performance_evidence import audit_cell,digest
from experiments.heldout_tasks.performance_observer import daily_choices
from experiments.study_design.performance_release import PerformancePermission
from experiments.study_design.decision import score,scenarios
from experiments.issue_dashboard.offhost_engineering import verify_routes
from experiments.issue_dashboard.offhost_resources import verify_budget
from experiments.issue_dashboard.offhost_loaded_metrics import check_clock
from experiments.issue_dashboard.wal_observer import delta
from scripts.audit_endtoend_distribution import server_connections,reconcile
from scripts.offhost_connectivity import check_pair
from experiments.issue_dashboard.path_controls import identity,body_for


def load(path):return json.loads(gzip.decompress(path.read_bytes()) if path.suffix=='.gz' else path.read_bytes())


def budget(value,expected):
    verify_budget(value)
    for sample in value['samples']:
        require(set(sample['groups'])==set(expected),'resource group coverage differs')
        for name,group in sample['groups'].items():
            require(all(row['allowed_cpus']==row['expected_cpus']==expected[name] for row in group['tasks']),'resource task mask differs from fixed host assignment')


def audit(target,observer,permission):
    t,o=(load(p/'report.json') for p in (target,observer));task=t['task'];day=t['collection_date_utc'];smoke=t['smoke'];values=cells(permission.artifacts,task,day,smoke)
    require(t['status']==o['status']=='passed' and o['task']==task and o['collection_date_utc']==day and o['smoke']==smoke and
        t['cases']==o['cases']==values and len(t['cells'])==len(o['cells'])==len(values),'complete paired full matrix required')
    require(t['confirmatory_data']==o['confirmatory_data']==(not smoke),'qualification/main identity differs');check_pair(t['local'],o['local'])
    require(t['remote']==o['local'] and o['remote']==t['local'] and t['source_sha256']==o['source_sha256']==permission._source_sha,'peer or source-byte bindings differ')
    for report in (t,o):
        authorization=report['authorization'];require(authorization['status']=='passed' and authorization['additional_spending_limit']==0 and authorization['visibility']=='public' and
            authorization['repository']==permission.artifacts['sample_plan']['execution_repository'] and authorization['release_sha256']==permission.expected_sha256 and authorization['collection_date_utc']==day and
            authorization['run_attempt']=='1' and authorization['execution_source_commit']==report['local']['source_commit'] and authorization['github_run_id']==report['local']['github_run_id'],'public/date/source execution authorization differs')
        require(all(datetime.fromisoformat(report[k]).astimezone(timezone.utc).date().isoformat()==day for k in ('started_at_utc','finished_at_utc')),'retained attempt spans real UTC dates')
        require(len(report['local']['allowed_cpus'])>=4,'full public host CPU inventory missing')
    require(not t['gateway_errors'],'gateway control error');check_clock(load(observer/'browser-clock.json'))
    target_cpu,db_cpu=sorted(t['local']['allowed_cpus'])[:2];generator_cpu,browser_cpu=sorted(o['local']['allowed_cpus'])[:2]
    connections=load(target/'connection-observations.json');server=server_connections(connections,target_cpu);identities=load(target/'identity-observations.json')
    require(len(identities)==len(connections),'gateway request identity observations missing')
    for event,gateway_identity in zip(connections,identities):require(all(event[k]==gateway_identity[k] for k in ('connection_id','response_index','path','operation_id')),'gateway promoted identity differs')
    summaries=[];serial=[];panels=defaultdict(dict);qualifications=[];first_loaded_target=None;first_loaded_observer=None
    for case in values:
        td,od=target/case['name'],observer/case['name'];packet=load(td/'profile-packet.json');require(packet==load(od/'profile-packet.json'),'native profile packet changed in transit')
        require(packet['case']==case and packet['source_commit']==t['local']['source_commit'] and packet['release_sha256']==permission.expected_sha256,'packet source or case differs')
        require(packet['state']==load(td/'validation-state.json') and packet['database']==load(td/'database-after.json') and packet['preparation']==load(td/'preparation.json'),'packet differs from native state/settings/preparation')
        require(packet['backend_events']==[json.loads(line) for line in (td/'backend.jsonl').read_text().splitlines()],'packet differs from original native event journal')
        assignment=load(td/'budget-assignment.json');require(assignment=={'app_mask':[target_cpu],'db_mask':[db_cpu],'selected_host_cpus':[target_cpu,db_cpu],
            'full_host_cpus':t['local']['allowed_cpus'],'gateway_and_overlay_in_app_budget':True},'fixed app/database CPU assignment differs')
        before,after=load(td/'database.json'),packet['database'];pids={r['pid'] for r in before['backends']}
        require(len(pids)==4 and pids=={r['pid'] for r in after['backends']} and before['pool_size']==after['pool_size']==4 and before['prepare_threshold'] is after['prepare_threshold'] is None,'native pool changed')
        for setting in before['backends']+after['backends']:
            require(setting['postgres'].startswith('PostgreSQL 18.0') and setting['synchronous_commit']==setting['fsync']==setting['full_page_writes']=='on' and
                setting['statement_timeout']=='10s' and setting['track_wal_io_timing']==setting['track_io_timing']=='off','database version, durability or observation settings differ')
        catalog=after['explicit_sql_catalog']
        for key,text in catalog.items():require(hashlib.sha256(text.encode()).hexdigest()==key,'explicit SQL catalog digest differs')
        for event in packet['backend_events']:
            require(event['allowed_cpus']==[target_cpu],'native origin CPU mask differs')
            if event['backend'] is not None:
                native=event['backend'];require(native['backend_pid'] in pids and native['allowed_cpus']==[target_cpu] and native['explicit_execute_calls'],'native backend identity or SQL calls missing')
                require(all(r['sql_sha256'] in catalog and len(r['parameters_sha256'])==64 for r in native['explicit_execute_calls']),'native SQL catalog binding missing')
        windows=load(td/'windows.json');require(windows==packet['windows'] and len(windows)==(6 if case['phase']=='qualification' else 1),'measurement window budget differs')
        for window in windows:
            label=window['label'];require(0<int(window['start_monotonic_ns'])<=int(window['collection_finish_monotonic_ns'])<=int(window['counter_finish_monotonic_ns']),'native window order differs')
            require(load(td/(label+'-wal-delta.json'))==delta(load(td/(label+'-wal-before.json')),load(td/(label+'-wal-after.json')),False),'native WAL/counter replay differs')
            if window['instrumented']:
                budget(load(td/(label+'-budget.json')),{'gateway_controller':[target_cpu],'overlay':[target_cpu],'origin':[target_cpu],'web':[target_cpu],'postgres':[db_cpu]})
                observed={'generator_sampler':[generator_cpu],'overlay':[generator_cpu]}
                if case['phase']!='qualification':observed['loaded_browser']=[browser_cpu]
                budget(load(od/(label+'-budget.json')),observed)
        for directory in (td,od):verify_routes(load(directory/'routes.json'))
        warm=[{'id':i,'kind':'read'} for i in range(4)];warm_bundle=load(od/'warmup.json.gz');bundles=[{'bundle':warm_bundle,'plan':warm,'prefix':case['name']+'-warm','cpu':generator_cpu,'serial':True}]
        browser=None;main_bundle=None
        if case['phase']=='qualification':
            counts={}
            for pair in range(3):
                for enabled in qualification_order(day,int(case['name'].split('-')[1]),pair):
                    label=f'pair-{pair}-'+('on' if enabled else 'off');bundle=load(od/(label+'.json.gz'));bundles.append({'bundle':bundle,'prefix':case['name']+'-'+label,'cpu':generator_cpu,'closed':True})
                    require(bundle['phase']['duration_s']==5 and bundle['phase']['workers']==4,'observation qualification budget differs');counts[pair,enabled]=sum(r['outcome']=='completed' and r['complete_s']<=5 for r in bundle['operations'])
            import statistics
            pairs=[{'pair':i,'enabled':counts[i,True],'disabled':counts[i,False],'ratio':counts[i,True]/counts[i,False]} for i in range(3)]
            result={'case':case['name'],'pairs':pairs,'median_ratio':statistics.median(r['ratio'] for r in pairs)}
            require(result==load(od/'qualification.json') and result['median_ratio']>=.90,'qualification ratio gate differs');qualifications.append(result)
        else:
            main_bundle=load(od/'operations.json.gz');plan=trace_for(permission.artifacts,case);require(plan==load(od/'schedule.json'),'prospective matched workload differs')
            bundles.append({'bundle':main_bundle,'plan':plan,'prefix':case['name']+'-main','cpu':generator_cpu,'serial':case['phase']=='serial'})
            folder=od/('quiet' if case['phase']=='serial' else 'loaded');config=load(folder/'loaded-browser-config.json');report=load(folder/'loaded-browser.json');browser={'config':config,'report':report}
            require(config['case']==case and config['strategy']==case['strategy'] and config['prefix']==case['name'] and config['duration_s']==(10 if case['phase']=='serial' else 20) and
                config['offsets_s']==([.5,3.5,6.5] if case['phase']=='serial' else [3,9,15]) and config['deadline_ms']==4000 and config['max_dispatch_lag_s']==.1,'fixed cold browser budget differs')
            require(config['permission']['expected_sha256']==permission.expected_sha256 and report['permission']['release_sha256']==permission.expected_sha256,'browser actual-release gate differs')
            start=load(folder/'loaded-start.json');ready=load(folder/'loaded-ready.json');completed=load(folder/'loaded-completed.json');launch=load(folder/'loaded-launch.json')
            require(start==report['start_signal'] and ready['pid']==launch['pid'] and int(launch['before_ns'])<=int(ready['monotonic_ns'])<=int(launch['after_ns']) and
                int(ready['monotonic_ns'])<=int(start['measurement_start_monotonic_ns'])<=int(report['probes_finished_monotonic_ns'])<=int(completed['monotonic_ns'])<=int(report['release_monotonic_ns']) and ready['contexts']==completed['probes']==3,'browser IPC/clock coverage differs')
            if case['phase']=='loaded':
                require(start['measurement_start_monotonic_ns']==main_bundle['phase']['start_monotonic_ns'] and main_bundle['phase']['workers']==16 and main_bundle['phase']['max_dispatch_lag_s']==.05 and main_bundle['phase']['drain_timeout_s']==30,'matched open-loop browser/load start differs')
                if first_loaded_target is None:first_loaded_target=int(windows[0]['start_monotonic_ns']);first_loaded_observer=int(start['measurement_start_monotonic_ns'])
            for row in report['probes']:
                for response in row['responses']:
                    if response['resource_type']=='document' or response['operation_id']:
                        key=response['operation_id'],response['method'],response['path'];matches=[e for e in server[key] if e['connection_id']==response['connection_id']]
                        require(len(matches)==1 and all(matches[0][k]==response[k] for k in ('status','body_bytes','body_sha256')),'browser actual response differs from gateway journal')
                    if response['path'].startswith('/api/'):
                        raw=(json.dumps(response['json'],sort_keys=True,separators=(',',':'),ensure_ascii=True)+'\n').encode();require(len(raw)==response['body_bytes'] and hashlib.sha256(raw).hexdigest()==response['body_sha256'],'browser API body evidence changed')
            offers=[json.loads(line) for line in (folder/'request-offers.jsonl').read_text().splitlines()]
            require(offers==[{'probe_id':row['id'],**request} for row in report['probes'] for request in row['requests']],'append-only browser offers differ')
        for entry in bundles:
            for session in entry['bundle']['sessions']:
                for receipt in session['records']:reconcile(receipt,server,generator_cpu)
        path_events=[json.loads(line) for line in (td/'path-controls.jsonl').read_text().splitlines()]
        require(len(path_events)==(6 if smoke else 32),'native path control budget differs')
        for window in ('pre','post'):
            path=load(od/('path-'+window+'.json'));receipts=path['records'];count=3 if smoke else 16
            require(path['window']==window and len(receipts)==count and 0<int(path['start_monotonic_ns'])<=int(path['finish_monotonic_ns']),'path-control identity or clocks differ')
            native=[r for r in path_events if r['window']==window];require(len(native)==count,'native path window missing')
            for index,(receipt,event) in enumerate(zip(receipts,native)):
                expected=identity(t['local']['run_id'],case['name'],window,index);raw=body_for(expected)
                require(all(event[k]==v for k,v in expected.items()) and event['allowed_cpus']==[target_cpu] and event['http_version']=='HTTP/1.1' and event['tcp_nodelay_raw']==1,'native matched-path contract differs')
                require(event['body_bytes']==len(raw) and event['body_sha256']==hashlib.sha256(raw).hexdigest() and receipt['body_sha256']==event['body_sha256'] and receipt['status']==200,'path-control body differs')
                reconcile(receipt,server,generator_cpu)
            if browser:
                bound=int(load(folder/'loaded-launch.json')['before_ns']) if window=='pre' else int(report['release_monotonic_ns'])
                require(int(path['finish_monotonic_ns'])<=bound if window=='pre' else int(path['start_monotonic_ns'])>=bound,'path control overlaps browser window')
        result=audit_cell(case,packet,bundles,browser);require(result==load(od/'business-audit.json'),'online and independent business audits differ')
        require(t['cells'][len(summaries)]['packet_sha256']==digest(packet) and o['cells'][len(summaries)]['business_audit_sha256']==digest(result),'case evidence summary hashes differ')
        if case['phase']=='serial':serial.append({'case':case,'packet':packet,'bundle':main_bundle,'browser_report':report,'audit':result})
        if case['phase']=='loaded':panels[case['context_id']][case['candidate']+'/'+case['strategy']]=[score(main_bundle['operations'],result['browser']['probes'],s,integrity_verified=True,ambiguous_writes=bool(result['ambiguous_writes'])) for s in scenarios()]
        summaries.append({'case':case['name'],'phase':case['phase'],'offered':result['operations'],'completed':result['completed'],'ambiguous_writes':result['ambiguous_writes'],'browser':result['browser']})
    require(load(observer/'qualification.json')==qualifications,'full qualification registry differs')
    profiles,choices=daily_choices(permission.artifacts,task,t['local']['source_commit'],serial)
    require(profiles==load(observer/'daily-profiles.json') and choices==load(observer/'daily-choices.json'),'policy choice replay differs')
    commitment=load(target/'policy-commitment.json');offer=load(observer/'policy-commitment-offer.json')
    require(commitment==load(observer/'policy-commitment.json') and commitment['choices_sha256']==digest(choices) and commitment['profiles_sha256']==digest(profiles) and
        commitment['release_sha256']==permission.expected_sha256 and commitment['collection_date_utc']==day and commitment['completed_cases']==18 and commitment['loaded_cases_started']==offer['loaded_cases_started']==0 and
        0<int(commitment['target_monotonic_ns'])<first_loaded_target and 0<int(offer['observer_monotonic_ns'])<first_loaded_observer,'policy was not committed before loaded outcomes')
    return {'status':'passed','role':'independent frozen reserved paired evaluation replay','task':task,'collection_date_utc':day,'smoke':smoke,'source_commit':t['local']['source_commit'],
        'github_run_id':t['local']['github_run_id'],'release_sha256':permission.expected_sha256,'cells':summaries,'panels':dict(panels),'choices':choices,
        'independent_unit':'UTC collection-day aggregate across both applications; this is one application component',
        'auditor_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('target','observer','output'):parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--release',required=True);parser.add_argument('--release-sha256',required=True);args=parser.parse_args()
    result=audit(args.target,args.observer,PerformancePermission(ROOT,args.release,args.release_sha256));args.output.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps({'status':'passed','cells':len(result['cells']),'task':result['task']}))


if __name__=='__main__':main()
