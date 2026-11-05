// Real reservation/event actions and rendered semantics; no performance estimates.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict'),crypto=require('node:crypto');
const {createRequire}=require('node:module');
const bundled=createRequire((process.env.QRL_NODE_MODULES||path.resolve(__dirname,'../../apps/issue-dashboard/node_modules'))+'/runtime.cjs');
const {chromium}=bundled('playwright');

const {capture}=require('./dom.cjs');

async function main(){
  const config=JSON.parse(fs.readFileSync(process.argv[2],'utf8')),endpoint=new URL(config.app_url);
  assert.equal(endpoint.protocol,'http:');assert.equal(endpoint.hostname,'127.0.0.1');assert(['inventory','analytics'].includes(config.task));
  const report={role:'reserved-task browser correctness',comparative_timing:false,task:config.task,query_kind:config.query_kind,probes:[],status:'started'};
  const browser=await chromium.launch({headless:true,...(process.env.QRL_CHROME?{executablePath:process.env.QRL_CHROME}:{})});
  try{
    report.browser=browser.version();
    for(const strategy of ['ssr','csr']){
      const row={strategy,operation_id:`${config.prefix}-${strategy}`,expected_failure:Boolean(config.failure),evidence:[],actions:[],page_errors:[],request_failures:[],api_reads:0,responses:[],response_errors:[]};report.probes.push(row);
      const pending=[];
      const context=await browser.newContext({viewport:{width:1280,height:900},serviceWorkers:'block'});
      try{
        await context.addInitScript(()=>{window.trustedTaskClicks=[];document.addEventListener('click',event=>{
          if(['primary-action','secondary-action'].includes(event.target.id))window.trustedTaskClicks.push({id:event.target.id,trusted:event.isTrusted});
        },true);
          window.taskPaintEntries=[];
          if(PerformanceObserver.supportedEntryTypes.includes('element'))new PerformanceObserver(list=>{
            for(const entry of list.getEntries())window.taskPaintEntries.push({identifier:entry.identifier,text:entry.element?.textContent,
              visible:entry.renderTime>0&&entry.intersectionRect.width>0&&entry.intersectionRect.height>0});
          }).observe({type:'element',buffered:true});
        });
        const correctnessTimeout=config.correctness_timeout_ms??5000;
        assert(Number.isSafeInteger(correctnessTimeout)&&correctnessTimeout>=5000&&correctnessTimeout<=120000);
        const page=await context.newPage();page.setDefaultTimeout(correctnessTimeout);
        page.on('pageerror',error=>row.page_errors.push(error.message));page.on('requestfailed',request=>row.request_failures.push({path:new URL(request.url()).pathname,error:request.failure()?.errorText}));
        page.on('request',request=>{if(new URL(request.url()).pathname==='/api/task')row.api_reads++;});
        page.on('response',response=>{pending.push((async()=>{
          const url=new URL(response.url());assert.equal(url.origin,endpoint.origin);const body=await response.body();
          row.responses.push({path:url.pathname,method:response.request().method(),operation_id:response.request().headers()['x-qrl-operation'],
            status:response.status(),body_bytes:body.length,body_sha256:crypto.createHash('sha256').update(body).digest('hex'),
            ...(url.pathname.startsWith('/api/')?{json:JSON.parse(body.toString('utf8'))}:{})});
        })().catch(error=>row.response_errors.push({error_type:error.name,error:error.message})));});
        const response=await page.goto(config.app_url+'/task/'+strategy+'?'+new URLSearchParams({operation_id:row.operation_id}),{waitUntil:'domcontentloaded'});assert.equal(response.status(),200);const html=await response.text();
        await page.locator('main[data-qrl-outcome="completed"],main[data-qrl-outcome="failed"]').waitFor();
        if(config.failure){
          await page.locator('main[data-qrl-error="origin_http_failure"]').waitFor();assert.equal(await page.locator('main').getAttribute('data-upstream-status'),'503');
          assert.equal(await page.locator('article').count(),0);assert.equal(await page.locator('#primary-action').isDisabled(),true);assert.equal(row.api_reads,strategy==='csr'?1:0);
          row.failure_dom=await page.evaluate(()=>({text:document.querySelector('#error').textContent,outcome:document.querySelector('main').dataset.qrlOutcome,
            error:document.querySelector('main').dataset.qrlError,upstream_status:document.querySelector('main').dataset.upstreamStatus,
            articles:document.querySelectorAll('article').length,primary_disabled:document.querySelector('#primary-action').disabled,secondary_disabled:document.querySelector('#secondary-action').disabled}));
        }else{
          await page.locator('main[data-ready="true"]').waitFor();assert.equal(html.includes('<article'),strategy==='ssr');assert.equal(row.api_reads,strategy==='csr'?1:0);
          row.evidence.push(await capture(page,config.task));
          if(config.paint_contract){
            const first=row.evidence[0].visible_text,expected={'task-count':first.count,'task-first-title':first.titles[0],'task-first-value':first.values[0]};
            assert(Object.values(expected).every(value=>typeof value==='string'));
            try{
              await page.waitForFunction(expected=>Object.entries(expected).every(([identifier,text])=>window.taskPaintEntries.some(e=>e.identifier===identifier&&e.text===text&&e.visible)),expected,{timeout:10000});
            }catch(error){row.paint_contract_observed=await page.evaluate(()=>window.taskPaintEntries);row.paint_contract_expected=expected;throw error;}
            row.paint_contract=await page.evaluate(expected=>Object.entries(expected).map(([identifier,text])=>window.taskPaintEntries.find(e=>e.identifier===identifier&&e.text===text&&e.visible)),expected);
          }
          await page.locator('#primary-action').click();await page.waitForFunction(()=>!document.querySelector('#secondary-action').disabled);
          let action=await page.locator('main').getAttribute('data-last-action');row.actions.push(JSON.parse(action));assert.equal(row.actions[0].result.changed,true);
          row.evidence.push(await capture(page,config.task));
          await page.locator('#secondary-action').click();await page.waitForFunction(()=>{
            const main=document.querySelector('main'),value=JSON.parse(main.dataset.lastAction||'null');
            return value?.request.kind===({inventory:'cancel',analytics:'refund'}[main.dataset.task])&&!document.querySelector('#primary-action').disabled;
          });
          action=await page.locator('main').getAttribute('data-last-action');row.actions.push(JSON.parse(action));assert.equal(row.actions[1].result.changed,true);
          row.evidence.push(await capture(page,config.task));
          assert.equal(await page.locator('#secondary-action').isDisabled(),true);assert.equal(await page.locator('main').getAttribute('data-action-error'),null);
          row.trusted_clicks=await page.evaluate(()=>window.trustedTaskClicks);assert.deepEqual(row.trusted_clicks,[{id:'primary-action',trusted:true},{id:'secondary-action',trusted:true}]);
          assert.equal(row.api_reads,(strategy==='csr'?1:0)+2);assert.equal(row.evidence[0].read_operation,config.task==='inventory'?'inventory_page':'event_time_groups');
          await page.screenshot({path:path.join(config.directory,`${strategy}.png`)});
        }
        await Promise.all(pending);assert.deepEqual(row.response_errors,[]);
        assert.deepEqual(row.page_errors,[]);assert.deepEqual(row.request_failures,[]);row.checks_passed=true;
      }finally{await context.close();}
    }report.status='passed';
  }finally{await browser.close();fs.writeFileSync(config.report_path,JSON.stringify(report,null,2)+'\n');}
}
main().catch(error=>{console.error(error);process.exitCode=1;});
