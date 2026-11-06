// Check the actual privilege-drop/cpuset browser path without application traffic.
const fs=require('node:fs'),path=require('node:path'),readline=require('node:readline');
const assert=require('node:assert/strict'),{createRequire}=require('node:module');
async function main(){
  const config=JSON.parse(fs.readFileSync(process.argv[2],'utf8')),root=path.resolve(__dirname,'../..');
  assert.equal(config.runtime_check_only,true);assert(process.getuid()>0);assert(process.getgid()>0);
  const bundled=createRequire(path.join(root,'apps/issue-dashboard/node_modules/runtime.cjs')),{chromium}=bundled('playwright');
  const browser=await chromium.launch({headless:true,args:['--no-proxy-server']});
  try{
    const context=await browser.newContext(),page=await context.newPage();let requests=0;
    page.on('request',()=>requests++);await page.setContent('<main>Browser runtime check</main>');
    assert.equal(await page.locator('main').innerText(),'Browser runtime check');assert.equal(requests,0);
    const report={status:'ready',uid:process.getuid(),gid:process.getgid(),pid:process.pid,browser:browser.version(),application_requests_sent:0,
      cgroup:fs.readFileSync('/proc/self/cgroup','utf8').trim(),local_content_verified:true,comparative_performance_acquired:false};
    fs.writeFileSync(config.runtime_result_path,JSON.stringify(report,null,2)+'\n');
    const input=readline.createInterface({input:process.stdin});
    const stopped=new Promise(resolve=>{input.on('line',line=>{assert.equal(JSON.parse(line).abort,true);resolve();});input.on('close',resolve);process.once('SIGTERM',resolve);});
    fs.writeFileSync(config.ready_path,JSON.stringify({pid:process.pid,monotonic_ns:String(process.hrtime.bigint())})+'\n');
    const timer=setTimeout(()=>{process.exitCode=1;input.close();},120000);
    await stopped;clearTimeout(timer);input.close();await browser.close();report.status='completed';
    fs.writeFileSync(config.runtime_result_path,JSON.stringify(report,null,2)+'\n');
  }finally{await browser.close();}
}
main().catch(error=>{console.error(error);process.exitCode=1;});
