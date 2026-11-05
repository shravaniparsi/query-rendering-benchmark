// Frozen-release/date-gated cold task probes with actual business mutations.
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto'),readline=require('node:readline');
const assert=require('node:assert/strict'),{execFileSync}=require('node:child_process'),{createRequire}=require('node:module');
const {capture}=require('./dom.cjs');
const now=()=>process.hrtime.bigint(),sleep=ms=>new Promise(resolve=>setTimeout(resolve,Math.max(0,ms)));

async function main(){
  const configPath=process.argv[2],config=JSON.parse(fs.readFileSync(configPath,'utf8')),endpoint=new URL(config.app_url),root=path.resolve(__dirname,'../..');
  assert.equal(endpoint.protocol,'http:');assert.equal(endpoint.port,'8765');assert(/^100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\./.test(endpoint.hostname));
  assert(['inventory','analytics'].includes(config.case.task));assert(['ssr','csr'].includes(config.strategy));assert.equal(config.case.strategy,config.strategy);
  const permission=JSON.parse(execFileSync(config.python_executable,[path.join(root,'scripts/check_reserved_performance_permission.py'),configPath],{encoding:'utf8',timeout:60000}));
  assert.equal(permission.status,'passed');assert.equal(permission.release_sha256,config.permission.expected_sha256);
  const bundled=createRequire((process.env.QRL_NODE_MODULES||path.join(root,'apps/issue-dashboard/node_modules'))+'/runtime.cjs'),{chromium}=bundled('playwright');
  const report={role:'reserved cold-context task probes',permission,task:config.case.task,strategy:config.strategy,probes:[],status:'started'};
  const browser=await chromium.launch({headless:true,args:['--no-proxy-server'],...(process.env.QRL_CHROME?{executablePath:process.env.QRL_CHROME}:{})});report.browser=browser.version();
  const input=readline.createInterface({input:process.stdin});let startResolve,startReject,releaseResolve,abortResolve,aborted=false;
  const startSignal=new Promise((resolve,reject)=>{startResolve=resolve;startReject=reject;}),releaseSignal=new Promise(resolve=>{releaseResolve=resolve;}),abortSignal=new Promise(resolve=>{abortResolve=resolve;});startSignal.catch(()=>{});
  const abort=()=>{aborted=true;startReject(new Error('parent aborted'));releaseResolve();abortResolve();browser.close().catch(()=>{});};
  process.once('SIGTERM',()=>{abort();browser.close().finally(()=>process.exit(1));});
  input.on('line',line=>{try{const value=JSON.parse(line);if(value.abort)abort();else if(value.release)releaseResolve();else startResolve(value);}catch(error){startReject(error);}});
  const prepared=[];
  function save(){fs.writeFileSync(config.report_path,JSON.stringify(report,null,2)+'\n');}
  try{
    for(const [id,offset] of config.offsets_s.entries()){
      const row={id,operation_id:`${config.prefix}-loaded-${id}`,strategy:config.strategy,intended_offset_s:offset,cold_context:true,outcome:'failed',checks_passed:false,
        page_errors:[],request_failures:[],http_errors:[],response_errors:[],api_requests:0,requests:[],responses:[],evidence:[],actions:[],integrity_error:false};
      const pending=[],context=await browser.newContext({viewport:{width:1280,height:900},serviceWorkers:'block'});
      await context.addInitScript(()=>{
        window.taskProbe={paints:[],trusted_clicks:[],supported:PerformanceObserver.supportedEntryTypes};
        if(PerformanceObserver.supportedEntryTypes.includes('element'))new PerformanceObserver(list=>{for(const entry of list.getEntries())window.taskProbe.paints.push({identifier:entry.identifier,renderTime:entry.renderTime,
          text:entry.element?.textContent,rect:entry.intersectionRect.toJSON()});}).observe({type:'element',buffered:true});
        document.addEventListener('click',event=>{if(['primary-action','secondary-action'].includes(event.target.id))window.taskProbe.trusted_clicks.push({id:event.target.id,trusted:event.isTrusted});},true);
      });
      const page=await context.newPage();page.setDefaultTimeout(config.deadline_ms);
      page.on('pageerror',error=>row.page_errors.push(error.message));page.on('requestfailed',request=>row.request_failures.push({path:new URL(request.url()).pathname,error:request.failure()?.errorText}));
      page.on('request',request=>{
        const url=new URL(request.url());if(url.pathname==='/api/task')row.api_requests++;
        const operation=request.headers()['x-qrl-operation']||(request.resourceType()==='document'?url.searchParams.get('operation_id'):null);
        const value={path:url.pathname,query:url.search,method:request.method(),operation_id:operation,resource_type:request.resourceType(),monotonic_ns:String(now()),post_data:request.postData()};row.requests.push(value);
        fs.appendFileSync(config.requests_path,JSON.stringify({probe_id:id,...value})+'\n');
      });
      page.on('response',response=>{pending.push((async()=>{
        const request=response.request(),url=new URL(response.url());assert.equal(url.origin,endpoint.origin);const headers=await response.allHeaders(),body=await response.body();
        assert.match(headers['x-qrl-connection'],/^[0-9a-f]{32}$/);if(response.status()>=400)row.http_errors.push({path:url.pathname,status:response.status()});
        row.responses.push({path:url.pathname,query:url.search,method:request.method(),operation_id:request.headers()['x-qrl-operation']||(request.resourceType()==='document'?url.searchParams.get('operation_id'):null),
          status:response.status(),connection_id:headers['x-qrl-connection'],body_bytes:body.length,body_sha256:crypto.createHash('sha256').update(body).digest('hex'),resource_type:request.resourceType(),
          ...(url.pathname.startsWith('/api/')?{json:JSON.parse(body.toString('utf8'))}:{})});
      })().catch(error=>row.response_errors.push({error_type:error.name,error:error.message.slice(0,300)})));});
      prepared.push({row,context,page,pending});
    }
    fs.writeFileSync(config.ready_path,JSON.stringify({pid:process.pid,monotonic_ns:String(now()),contexts:prepared.length})+'\n');
    const signal=await startSignal;report.start_signal=signal;const start=BigInt(signal.measurement_start_monotonic_ns);assert.equal(signal.duration_s,config.duration_s);
    for(const item of prepared){
      const {row,context,page,pending}=item;report.probes.push(row);const due=start+BigInt(Math.round(row.intended_offset_s*1e9));
      while(now()<due)await Promise.race([sleep(Number(due-now())/1e6),abortSignal.then(()=>{throw new Error('parent aborted');})]);if(aborted)throw new Error('parent aborted');
      row.dispatch_monotonic_ns=String(now());row.actual_offset_s=Number(BigInt(row.dispatch_monotonic_ns)-start)/1e9;
      if(row.actual_offset_s-row.intended_offset_s>config.max_dispatch_lag_s){row.outcome='dispatch_lag_drop';row.error_type='BrowserDispatchLag';row.finished_monotonic_ns=String(now());save();continue;}
      let timer,work;
      try{
        work=(async()=>{
          const response=await page.goto(config.app_url+'/task/'+config.strategy+'?'+new URLSearchParams({operation_id:row.operation_id}),{waitUntil:'domcontentloaded',timeout:config.deadline_ms});
          row.document_status=response.status();row.document_html=await response.text();if(response.status()!==200)throw new Error('document_http_failure');
          await page.locator('main[data-qrl-outcome="completed"],main[data-qrl-outcome="failed"]').waitFor();row.app_outcome=await page.locator('main').getAttribute('data-qrl-outcome');
          if(row.app_outcome!=='completed')throw new Error('application_failure');assert.equal(row.document_html.includes('<article'),config.strategy==='ssr');
          const initial=await capture(page,config.case.task);row.evidence.push(initial);
          const text=initial.visible_text,expected={'task-count':text.count,'task-first-title':text.titles[0],'task-first-value':text.values[0]};assert(Object.values(expected).every(value=>typeof value==='string'));
          await page.waitForFunction(expected=>Object.entries(expected).every(([identifier,text])=>window.taskProbe.paints.some(e=>e.identifier===identifier&&e.text===text&&e.renderTime>0&&e.rect.width>0&&e.rect.height>0)),expected);
          row.paint_entries=await page.evaluate(()=>window.taskProbe.paints);
          const entries=Object.entries(expected).map(([identifier,text])=>row.paint_entries.find(e=>e.identifier===identifier&&e.text===text&&e.renderTime>0&&e.rect.width>0&&e.rect.height>0));
          row.critical_content_paint_ms=Math.max(...entries.map(e=>e.renderTime));save();
          await page.locator('main[data-ready="true"]').waitFor();await page.locator('#primary-action').click();await page.waitForFunction(()=>!document.querySelector('#secondary-action').disabled);
          row.actions.push(JSON.parse(await page.locator('main').getAttribute('data-last-action')));assert.equal(row.actions[0].result.changed,true);row.evidence.push(await capture(page,config.case.task));save();
          await page.locator('#secondary-action').click();await page.waitForFunction(()=>{const main=document.querySelector('main'),value=JSON.parse(main.dataset.lastAction||'null');return value?.request.kind===({inventory:'cancel',analytics:'refund'}[main.dataset.task])&&!document.querySelector('#primary-action').disabled;});
          row.actions.push(JSON.parse(await page.locator('main').getAttribute('data-last-action')));assert.equal(row.actions[1].result.changed,true);row.evidence.push(await capture(page,config.case.task));
          assert.equal(await page.locator('#secondary-action').isDisabled(),true);assert.equal(await page.locator('main').getAttribute('data-action-error'),null);
          row.trusted_clicks=await page.evaluate(()=>window.taskProbe.trusted_clicks);assert.deepEqual(row.trusted_clicks,[{id:'primary-action',trusted:true},{id:'secondary-action',trusted:true}]);
          assert.equal(row.api_requests,(config.strategy==='csr'?1:0)+2);assert.deepEqual(row.page_errors,[]);assert.deepEqual(row.request_failures,[]);assert.deepEqual(row.http_errors,[]);
          row.navigation=await page.evaluate(()=>performance.getEntriesByType('navigation').map(e=>({responseStart:e.responseStart,responseEnd:e.responseEnd,domContentLoadedEventEnd:e.domContentLoadedEventEnd,transferSize:e.transferSize})));
          row.outcome='completed';row.checks_passed=true;
        })();
        await Promise.race([work,new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error('probe_deadline')),config.deadline_ms);})]);
      }catch(error){
        row.outcome='failed';row.checks_passed=false;row.error_type=error.name;row.error=error.message.slice(0,500);
        if(/Actual visible|Visible analytics count|Unexpected task|Unsafe integer|AssertionError/.test(error.message)||error.name==='AssertionError')row.integrity_error=true;
        if(error.message==='probe_deadline')await context.close();if(work)await work.catch(()=>{});row.outcome='failed';row.checks_passed=false;
      }finally{
        clearTimeout(timer);await Promise.all(pending);
        if(row.outcome==='completed'){
          try{assert.deepEqual(row.response_errors,[]);row.protocols=await page.evaluate(()=>[...performance.getEntriesByType('navigation'),...performance.getEntriesByType('resource')].map(e=>e.nextHopProtocol).filter(Boolean));assert(row.protocols.length>0&&row.protocols.every(v=>v==='http/1.1'));}
          catch(error){row.outcome='failed';row.checks_passed=false;row.error_type=error.name;row.error=error.message.slice(0,500);row.integrity_error=true;}
        }
        row.finished_monotonic_ns=String(now());save();
      }
    }
    report.probes_finished_monotonic_ns=String(now());fs.writeFileSync(config.completed_path,JSON.stringify({monotonic_ns:String(now()),probes:report.probes.length})+'\n');
    await releaseSignal;if(aborted)throw new Error('parent aborted');report.release_monotonic_ns=String(now());const first=prepared.find(item=>item.row.outcome==='completed');
    if(first){await first.page.screenshot({path:config.screenshot_path});report.screenshot_probe_id=first.row.id;}report.status='completed';
  }finally{input.close();await browser.close();save();}
}
main().catch(error=>{console.error(error);process.exitCode=1;});
