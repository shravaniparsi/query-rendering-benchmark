// Inject one refresh HTTP failure after a real mutation; correctness only.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict'),{createRequire}=require('node:module');
const {capture}=require('./dom.cjs'),{settleAction}=require('./performance_browser.cjs');
async function main(){
  const config=JSON.parse(fs.readFileSync(process.argv[2],'utf8')),endpoint=new URL(config.app_url);
  assert.equal(endpoint.hostname,'127.0.0.1');assert.equal(endpoint.protocol,'http:');assert(['inventory','analytics'].includes(config.task));
  const {chromium}=createRequire((process.env.QRL_NODE_MODULES||path.resolve(__dirname,'../../apps/issue-dashboard/node_modules'))+'/runtime.cjs')('playwright');
  const browser=await chromium.launch({headless:true,...(process.env.QRL_CHROME?{executablePath:process.env.QRL_CHROME}:{})});
  const report={role:'injected refresh failure correctness',comparative_timing:false,performance_acquisition:false,task:config.task,probes:[],status:'started'};
  try{
    for(const strategy of ['ssr','csr']){
      const context=await browser.newContext({viewport:{width:1280,height:900},serviceWorkers:'block'});
      try{
        const page=await context.newPage();page.setDefaultTimeout(30000);const row={strategy,initial:null,action:null,injected_responses:[]};report.probes.push(row);
        await page.goto(config.app_url+'/task/'+strategy+'?'+new URLSearchParams({operation_id:config.prefix+'-'+strategy}),{waitUntil:'domcontentloaded'});
        await page.locator('main[data-ready="true"]').waitFor();row.initial=await capture(page,config.task);
        await page.route('**/api/task?*',async route=>{
          row.injected_responses.push({operation_id:route.request().headers()['x-qrl-operation'],status:503,explicit_injection:true});
          await route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({ok:false,error:{code:'origin_http_failure',upstream_status:503}})});
        });
        await page.locator('#primary-action').click();
        try{await settleAction(page,config.task,false);assert.fail('failed refresh was accepted');}
        catch(error){assert.equal(error.name,'BrowserTaskApplicationFailure');row.failure_state=error.failure_state;}
        assert.equal(row.injected_responses.length,1);row.action=JSON.parse(await page.locator('main').getAttribute('data-last-action'));
        assert.equal(row.action.result.changed,true);assert.equal(await page.locator('article').count(),0);
        assert.equal(await page.locator('#primary-action').isDisabled(),true);assert.equal(await page.locator('#secondary-action').isDisabled(),true);
        // A copied corruption of the failed view must still trigger semantic rejection.
        await page.evaluate(()=>{document.querySelector('#primary-action').disabled=false;});
        await assert.rejects(()=>settleAction(page,config.task,false),{name:'AssertionError'});row.active_control_corruption_rejected=true;row.checks_passed=true;
      }finally{await context.close();}
    }
    report.status='passed';
  }finally{await browser.close();fs.writeFileSync(config.report_path,JSON.stringify(report,null,2)+'\n');}
}
main().catch(error=>{console.error(error);process.exitCode=1;});
