"""Collect serial HTTP requests and subsequent quiet browser probes on fresh fixtures."""
import os,time
from scripts.offhost_connectivity import network_state,check_pair
from experiments.query_rendering_load.arrivals import execute
from experiments.query_rendering_load.calibrate import summarize
from .offhost_resources import BudgetWatch,browser
from .offhost_engineering import RouteWatch,verify_routes,save,request_json,require
from .cpu_budget import bind_current,bind_overlay,overlay_pid,tree
from .arrivals import operation,schedule
from .loaded_browser import LoadedBrowser
from .offhost_serial_design import design,cases_for,serial_plan,SEED_BASE
from .serial_profile_metrics import execute_serial


def run_observer(output,own,peer_name,peer_ip,allocation,smoke,*,spec=None,cases=None,collected=None,handshake=True,finish=True,probe_callback=None):
    spec=design(smoke) if spec is None else spec;cases=cases_for(allocation,smoke) if cases is None else cases
    cpus=sorted(own['allowed_cpus'])[:2];require(len(cpus)==2,'two observer CPUs required');bind_current(cpus[:1]);overlay=overlay_pid();bind_overlay(overlay,cpus[:1])
    base=f'http://{peer_ip}:8765';deadline=time.monotonic()+180
    while True:
        try:remote=request_json(base,'/manifest',timeout=3);break
        except OSError:
            if time.monotonic()>deadline:raise
            time.sleep(1)
    check_pair(own,remote)
    if handshake:request_json(base,'/qrl/hello',own)
    cells=[] if collected is None else collected
    for case in cases:
        directory=output/case['name'];directory.mkdir();loaded=None;acquisition_started=time.perf_counter_ns()
        require(request_json(base,'/qrl/begin',{'case':case['name'],'observer':own})=={'case':case,'ready':True},'begin differs')
        network_state(own['overlay_name'],peer_name,timeout=30,require_direct=True);watch=RouteWatch(own['overlay_name'],peer_name).start()
        def capture():return {'generator_sampler':tree(os.getpid(),cpus[:1],False),'overlay':tree(overlay,cpus[:1],False)}
        try:
            browser(base,directory,case,'browser-before',cpus[1]);request_json(base,'/qrl/fault-on',{'case':case['name']})
            browser(base,directory,case,'browser-failure',cpus[1])
            save(directory/'logical-failure.json',operation({'id':0,'kind':'read','project_id':0,'state':'open','limit':20,'offset':0},case['strategy'],base,base,case['name']+'-logical-fault',allowed_remote_host=peer_ip))
            request_json(base,'/qrl/fault-off',{'case':case['name']})
            warm=schedule(spec['warmup_rate'],spec['warmup_s'],0,spec['rows'],SEED_BASE+allocation)
            warm_records=execute(warm,lambda r:operation(r,case['strategy'],base,base,case['name']+'-warm',allowed_remote_host=peer_ip),workers=4)
            save(directory/'warmup.json',warm_records);require(all(r['outcome']=='completed' for r in warm_records),'warmup incomplete')
            if probe_callback:probe_callback(base,directory,case,'pre',own,smoke)
            planned=serial_plan(spec,allocation);save(directory/'schedule.json',planned);request_json(base,'/qrl/measure-begin',{'case':case['name']})
            budget=BudgetWatch(capture).start();records=None
            try:
                records,phase=execute_serial(planned,lambda r:operation(r,case['strategy'],base,base,case['name'],allowed_remote_host=peer_ip),on_record=lambda value:save(directory/'operations.json',value))
                save(directory/'operations.json',records);save(directory/'serial-phase.json',phase)
            finally:save(directory/'http-budget-window.json',budget.finish())
            loaded=LoadedBrowser(base,directory,case,cpus[1],spec);loaded.launch()
            def browser_capture():return {**capture(),'loaded_browser':loaded.sample()}
            budget=BudgetWatch(browser_capture).start();started=time.perf_counter()
            try:
                loaded.start(started);time.sleep(spec['duration_s']);loaded.wait_completed()
            finally:save(directory/'budget-window.json',budget.finish())
            request_json(base,'/qrl/measure-end',{'case':case['name']});loaded.release()
            if probe_callback:probe_callback(base,directory,case,'post',own,smoke)
            request_json(base,'/qrl/visible-write',{'case':case['name']});browser(base,directory,case,'browser-after',cpus[1])
        finally:
            if loaded:loaded.close()
            routes=watch.finish();save(directory/'routes.json',routes)
        verify_routes(routes);request_json(base,'/qrl/end',{'case':case['name']})
        acquisition_finished=time.perf_counter_ns();phase.update(case_acquisition_start_ns=str(acquisition_started),case_acquisition_finish_ns=str(acquisition_finished),case_acquisition_wall_s=(acquisition_finished-acquisition_started)/1e9)
        save(directory/'serial-phase.json',phase)
        summary=summarize(records,phase['http_duration_s']);cells.append({**case,'summary':summary})
        print({'phase':'serial_cell_complete',**case,'outcomes':summary['outcomes']},flush=True)
    if finish:request_json(base,'/qrl/finish',{'observer':own})
    return remote,cells
