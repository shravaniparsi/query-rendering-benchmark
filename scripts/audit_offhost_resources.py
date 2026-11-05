"""Replay a paired off-host resource/WAL diagnostic from raw host artifacts."""
import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from experiments.issue_dashboard.offhost_engineering import require,verify_routes
from experiments.issue_dashboard.offhost_resources import verify_budget,mixed_plan
from experiments.issue_dashboard.cpu_budget import verify_browser_sample
from experiments.issue_dashboard.offhost_resource_design import ROLE,design,cases_for
from experiments.issue_dashboard.oracle import IssueOracle
from experiments.issue_dashboard.fixture import COUNT_SQL,QUERIES
from experiments.issue_dashboard.wal_observer import delta as wal_delta
from experiments.issue_dashboard.write_phase_metrics import validate_trace
from experiments.issue_dashboard.arrivals import schedule
from experiments.issue_dashboard.comparison_metrics import mutation_summaries,backend_summaries,verify_backend_coverage
from experiments.query_rendering_load.calibrate import summarize


def load(directory,name):return json.loads((directory/name).read_text())


def eligible(cell):
    groups=[cell['read_summary'],cell['write_summary'],*cell['mutation_summaries'].values()]
    return (not cell['summary']['generator_limited'] and not cell['summary']['censored'] and not cell['ambiguous_writes']
        and all(r['offered'] and r['outcomes'].get('completed',0)/r['offered']>=.99 for r in groups))


def usage(value):
    result={}
    for name in value['samples'][0]['groups']:
        a=value['samples'][0]['groups'][name]['processes'];b=value['samples'][-1]['groups'][name]['processes'];keys=set(a)&set(b)
        require(keys,'no matched CPU identities')
        totals={field:sum(b[k]['cpu_times'][field]-a[k]['cpu_times'][field] for k in keys) for field in ('user','system')}
        require(all(v>=0 and math.isfinite(v) for v in totals.values()),'invalid process CPU delta')
        result[name]={'matched_processes':len(keys),'first_only':len(set(a)-set(b)),'last_only':len(set(b)-set(a)),
                      'observed_cpu_seconds':sum(totals.values())}
    return result


def check_budget(value,expected):
    verify_budget(value)
    for sample in value['samples']:
        require(set(sample['groups'])==set(expected),'budget groups differ')
        for name,group in sample['groups'].items():
            for task in group['tasks']:
                require(task['expected_cpus']==expected[name] and task['allowed_cpus']==expected[name],'budget assignment mismatch')


def check_browser(value,case,label):
    require(len(value['probes'])==1,'browser probe budget differs');probe=value['probes'][0]
    require(probe['checks_passed'] and probe['strategy']==case['strategy'] and
            probe['api_requests']==int(case['strategy']=='csr'),'browser semantics differ')
    if label=='browser-failure':
        require(probe['expected_failure'] and probe['app_outcome']=='failed','expected browser failure missing');return probe
    require(not probe['expected_failure'] and probe['app_outcome']=='completed','positive browser missing')
    entries=[next(e for e in probe['paint_entries'] if e['identifier']==identifier)
             for identifier in ('issue-count','issue-first-title','issue-first-comments')]
    require(all(math.isfinite(e['renderTime']) and e['renderTime']>0 and e['rect']['width']>0 and e['rect']['height']>0 for e in entries),'paint bounds differ')
    require(entries[0]['text']==f'Issues: {probe["evidence"]["issuesCount"]}' and
            entries[1]['text']==probe['evidence']['issues'][0]['title'] and
            entries[2]['text']==f'Comments: {probe["evidence"]["issues"][0]["comment_count"]}','painted text differs')
    require(max(e['renderTime'] for e in entries)==probe['critical_content_paint_ms'],'paint arithmetic differs')
    return probe


def audit(target,observer,commit,metadata):
    a,b=(load(p,'report.json') for p in (target,observer));spec=design(a['smoke']);allocation=a['allocation'];cases=cases_for(allocation,a['smoke'])
    for role,report in zip(('target','observer'),(a,b)):
        require(report['status']=='passed' and report['role']==ROLE and report['confirmatory_data'] is False,'incomplete/wrong cohort')
        require(report['local']['role']==role and report['local']['source_commit']==commit,'source/role differs')
        require(report['allocation']==allocation and report['smoke']==a['smoke'] and report['design']==spec and report['cases']==cases,'matrix differs')
        require(len(report['cells'])==16 and all(all(row[k]==case[k] for k in case) for row,case in zip(report['cells'],cases)),'collected cells differ')
        for name,digest in report['source_sha256'].items():
            raw=subprocess.run(['git','show',f'{commit}:{name}'],cwd=ROOT,check=True,capture_output=True).stdout
            require(hashlib.sha256(raw).hexdigest()==digest,'source differs: '+name)
    require(a['source_sha256']==b['source_sha256'] and a['remote']==b['local'] and b['remote']==a['local'],'reciprocal/source identities differ')
    require(a['local']['kernel_boot_sha256']!=b['local']['kernel_boot_sha256'],'same VM kernel')
    require(str(metadata['databaseId'])==a['local']['run_id'] and metadata['headSha']==commit,'run metadata differs')
    require(metadata['conclusion']=='success' and len(metadata['jobs'])==3 and all(j['conclusion']=='success' for j in metadata['jobs']),'three successful jobs required')
    cleanup=[s for j in metadata['jobs'] for s in j['steps'] if s['name']=='Post Join dedicated research overlay with ephemeral workload identity']
    require(len(cleanup)==2 and all(s['conclusion']=='success' for s in cleanup),'overlay cleanup incomplete')
    require(not a['gateway_errors'],'gateway errors present')
    require(load(target.parent,'preflight.json')=={'run':85,'skipped':0,'failures':0,'errors':0,'status':'passed'},'target shared contracts incomplete')
    require(load(target.parent,'wal-preflight.json')=={'run':1,'skipped':0,'errors':0,'failures':0,'status':'passed'},'PG18 WAL ownership/timer contract incomplete')
    cpu_t=sorted(a['local']['allowed_cpus'])[:2];cpu_o=sorted(b['local']['allowed_cpus'])[:2]
    require(len(cpu_t)==len(cpu_o)==2,'two-CPU budgets unavailable');cells=[];totals=Counter()
    containment=load(observer.parent,'browser-cgroups.json')
    require(containment['role']=='browser cpuset containment setup' and
            containment['groups']['observer']['path']=='/sys/fs/cgroup/qrl_browser_observer' and
            containment['groups']['observer']['cpu']==cpu_o[1] and
            containment['groups']['observer']['effective_cpus']==str(cpu_o[1]),'browser kernel cpuset setup differs')
    for case,reported in zip(cases,b['cells']):
        t,o=target/case['name'],observer/case['name'];planned=load(o,'schedule.json');records=load(o,'operations.json');warm=load(o,'warmup.json')
        require(planned==mixed_plan(spec,allocation) and len(records)==len(planned),'scheduled work differs')
        require([r['id'] for r in records]==list(range(len(planned))),'operation identities differ')
        require(len(warm)==spec['warmup_rate']*spec['warmup_s'] and all(r['outcome']=='completed' for r in warm),'warmup incomplete')
        state=load(t,'validation-state.json');require((state['rows'],state['fanout'],state['query_kind'])==(spec['rows'],spec['fanout'],case['query_kind']),'fixture differs')
        oracle=IssueOracle(state);final=oracle.verify_final(state)
        for row,plan in zip(records,planned):
            require(all(row.get(k)==v for k,v in plan.items()),'offered row differs')
            require(row['outcome'] in ('completed','failed','admission_drop','dispatch_lag_drop','drain_timeout'),'unknown outcome')
            if row['outcome']=='completed':
                require(row['intended_s']<=row['dispatch_s']<=row['complete_s']<=row['validation_complete_s'],'observer clocks differ')
                if row['kind']=='read':oracle.verify(row['read_evidence'])
        for row in warm:oracle.verify(row['read_evidence'])
        control=load(t,'visible-control.json');payload=control['mutation']
        prior={**state,'ledger':[r for r in state['ledger'] if r['operation_id']!=payload['operation_id']]}
        require(IssueOracle(prior).issue_at(payload['issue_id'])==control['before'],'visible control starting state differs')
        require(control['ack']['changed'] and control['ack']['revision']==control['before']['revision']+1,'control acknowledgement differs')
        probes={label:check_browser(load(o,label+'.json'),case,label) for label in ('browser-before','browser-failure','browser-after')}
        for label in ('browser-before','browser-after'):oracle.verify(probes[label]['evidence'])
        first=probes['browser-after']['evidence']['issues'][0]
        require(first['id']==payload['issue_id'] and first['comment_count']==control['before']['comment_count']+1,'changed aggregate absent')
        failure=load(o,'logical-failure.json');require(failure['outcome']=='failed' and failure['failure_code']=='origin_http_failure' and failure['upstream_status']==503,'fault semantics differ')
        expected={f'{case["name"]}-{r["id"]}':{'issue_id':r['issue_id'],'kind':r['mutation'],'value':r['value']} for r in planned if r['kind']=='write'}
        expected[payload['operation_id']]={k:payload[k] for k in ('issue_id','kind','value')}
        actual={r['operation_id']:{k:r[k] for k in ('issue_id','kind','value')} for r in state['ledger']}
        acknowledged={f'{case["name"]}-{r["id"]}' for r in records if r['kind']=='write' and r['outcome']=='completed'}|{payload['operation_id']}
        require(acknowledged.issubset(actual) and all(k in expected and expected[k]==v for k,v in actual.items()),'committed writes differ')
        ambiguous=sorted(set(actual)-acknowledged)
        events=[json.loads(s) for s in (t/'backend.jsonl').read_text().splitlines()]
        allowed={f'{case["name"]}-{r["id"]}' for r in planned}|{f'{case["name"]}-warm-{r["id"]}' for r in warm}|{
            payload['operation_id'],*(case['name']+'-'+label for label in probes),case['name']+'-logical-fault-0'}
        required={f'{case["name"]}-{r["id"]}':{'status':200,'path':'/issues' if r['kind']=='read' else '/control/mutate',
            **({'snapshot':r['read_evidence']['snapshot']} if r['kind']=='read' else {})} for r in records if r['outcome']=='completed'}
        required.update({f'{case["name"]}-warm-{r["id"]}':{'status':200,'path':'/issues','snapshot':r['read_evidence']['snapshot']} for r in warm})
        required.update({case['name']+'-'+label:{'status':503 if label=='browser-failure' else 200,'path':'/issues',
            **({'snapshot':probe['evidence']['snapshot']} if label!='browser-failure' else {})} for label,probe in probes.items()})
        required[payload['operation_id']]={'status':200,'path':'/control/mutate','revision':control['ack']['revision']}
        required[case['name']+'-logical-fault-0']={'status':503,'path':'/issues'}
        verify_backend_coverage(events,required,allowed)
        wal=wal_delta(load(t,'wal-before.json'),load(t,'wal-after.json'),case['instrumented']);require(wal==load(t,'wal-delta.json'),'WAL delta differs')
        pids={r['pid'] for r in wal['backends']};query_digest=hashlib.sha256((COUNT_SQL+'\n'+QUERIES[case['query_kind']]).encode()).hexdigest();phases=0
        for event in events:
            require(event['query_kind']==case['query_kind'],'backend query differs')
            if event['path']=='/issues':require(event['read_query_sha256']==query_digest,'SQL identity differs')
            if event['path']=='/control/mutate' and event['status']==200:
                backend=event['backend'];require(backend['backend_pid'] in pids,'write used non-pool backend')
                require(('write_phase_trace' in backend)==case['instrumented'],'phase tracing differs')
                if case['instrumented']:validate_trace(backend,case['query_kind'],expected[event['operation_id']]['kind']);phases+=1
        masks_t=cpu_t if case['placement']=='shared' else cpu_t[:1];masks_db=cpu_t if case['placement']=='shared' else cpu_t[1:2]
        budgets_t=load(t,'budget-window.json');budgets_o=load(o,'budget-window.json')
        check_budget(budgets_t,{'gateway_controller':masks_t,'overlay':masks_t,'origin':masks_t,'web':masks_t,'postgres':masks_db})
        check_budget(budgets_o,{'generator_sampler':cpu_o[:1],'overlay':cpu_o[:1]})
        route_count=0;task_count=sum(len(g['tasks']) for value in (budgets_t,budgets_o) for s in value['samples'] for g in s['groups'].values())
        for directory,report in ((t,a),(o,b)):
            routes=load(directory,'routes.json');verify_routes(routes);route_count+=len(routes)
            for row in routes:
                net=row['network'];require(net['self']['HostName']==report['local']['overlay_name'] and net['peer']['HostName']==report['remote']['overlay_name'],'route peer differs')
        for label in probes:
            samples=load(o,label+'-placement.json');require(samples,'no browser task observations')
            for sample in samples:
                verify_browser_sample(sample,cpu_o[1])
                for task in sample['tasks']:
                    require(task['matches'] and task['allowed_cpus']==task['expected_cpus']==cpu_o[1:2],'browser mask differs');task_count+=1
        for settings in (load(t,'database.json')['service'],load(t,'database-after.json')):
            require(settings['write_phase_tracing']==case['instrumented'] and settings['durable_writes'],'instrumentation/durability differs')
            require(all(settings['durability'][key]=='on' for key in ('synchronous_commit','fsync','full_page_writes')),'durability differs')
        summary=summarize(records,spec['duration_s']);require(summary==reported['summary'],'outcome arithmetic differs')
        cell={**case,'summary':summary,'read_summary':summarize([r for r in records if r['kind']=='read'],spec['duration_s']),
            'write_summary':summarize([r for r in records if r['kind']=='write'],spec['duration_s']),
            'mutation_summaries':mutation_summaries(records,spec['duration_s']),
            'backend_summaries':backend_summaries(records,events,case['name']),'wal':wal,'ambiguous_writes':ambiguous,
            'cpu_window':{'target':usage(budgets_t),'observer':usage(budgets_o)},'final_oracle':final}
        cell['eligible']=eligible(cell);cells.append(cell)
        totals.update(operations=len(records),completed=summary['outcomes'].get('completed',0),committed_writes=len(actual),
            read_snapshots=sum(r['kind']=='read' and r['outcome']=='completed' for r in records),warmup_snapshots=len(warm),
            browser_snapshots=2,fault_checks=2,backend_identities=len(events),phase_profiles=phases,route_samples=route_count,
            task_masks=task_count,summary_counts_checked=final['summary_counts_checked'])
    return {'status':'passed','role':ROLE,'confirmatory_data':False,'source_commit':commit,'run_id':a['local']['run_id'],
            'allocation':allocation,'smoke':a['smoke'],'design':spec,'cells':cells,'totals':dict(totals),
            'auditor_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('target','observer','run-metadata','output'):parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--commit',required=True);args=parser.parse_args()
    result=audit(args.target,args.observer,args.commit,load(args.run_metadata.parent,args.run_metadata.name))
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result['totals'],indent=2))


if __name__=='__main__':main()
