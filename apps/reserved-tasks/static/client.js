/* Shared rendering and real reservation/event actions for synthetic tasks. */
const main=document.querySelector('main'),task=main.dataset.task;
const parameters=JSON.parse(document.querySelector('#parameters').textContent);
const primary=document.querySelector('#primary-action'),secondary=document.querySelector('#secondary-action');
let data=JSON.parse(document.querySelector('#initial-data').textContent),last=null,sequence=0,busy=false;
const prefix=main.dataset.operationPrefix||crypto.randomUUID();
function element(tag,text){const value=document.createElement(tag);if(text!==undefined)value.textContent=text;return value;}
function integer(value){if(!Number.isSafeInteger(value))throw new Error('Unsafe integer in response');return value;}
function money(value){integer(value);const magnitude=Math.abs(value);return (value<0?'-':'')+'$'+Math.floor(magnitude/100)+'.'+String(magnitude%100).padStart(2,'0');}
function fail(value){main.dataset.qrlOutcome='failed';main.dataset.ready='false';main.dataset.qrlError=value?.error?.code||'application_failure';main.dataset.upstreamStatus=String(value?.error?.upstream_status||'');document.querySelector('#error').textContent='Unable to load. Please try again.';document.querySelector('#items').replaceChildren();document.querySelector('#facets').replaceChildren();primary.disabled=true;secondary.disabled=true;}
function render(value){
  if(value?.ok!==true){fail(value);return;}
  const operation=task==='inventory'?'inventory_page':'event_time_groups';if(value.read_operation!==operation)throw new Error('Unexpected task response');
  const nodes=[];
  for(const [index,row] of (task==='inventory'?value.items:value.groups).entries()){
    const article=element('article');
    const title=element('h2',task==='inventory'?`Product ${integer(row.id)}`:value.parameters.group_by.map(key=>row[key]).join(' / '));
    const output=element('output',task==='inventory'?`Available: ${integer(row.remaining_stock)}`:`Net sales: ${money(row.net_total_minor)}`);
    output.dataset.value=String(task==='inventory'?row.remaining_stock:row.net_total_minor);
    const valueRow=element('p');valueRow.append(output);
    if(index===0){title.setAttribute('elementtiming','task-first-title');valueRow.setAttribute('elementtiming','task-first-value');}
    if(task==='inventory'){
      Object.assign(article.dataset,{id:String(row.id),category:row.category,brand:row.brand,priceMinor:String(integer(row.price_minor)),initialStock:String(integer(row.initial_stock)),remainingStock:String(row.remaining_stock)});
      const price=element('p',`Price: ${money(row.price_minor)}`);price.dataset.price='';article.append(title,element('p',`${row.category} · ${row.brand}`),price,valueRow);
    }else{
      Object.assign(article.dataset,{region:row.region||'',category:row.category||''});
      for(const key of ['gross_total_minor','refund_total_minor','sales_count','refund_count','net_total_minor'])article.dataset[key.replace(/_([a-z])/g,(_,c)=>c.toUpperCase())]=String(integer(row[key]));
      article.append(title,valueRow,element('p',`Sales: ${row.sales_count} · Refunds: ${row.refund_count}`));
    }
    nodes.push(article);
  }
  document.querySelector('#items').replaceChildren(...nodes);
  const count=element('p',task==='inventory'?`Products: ${integer(value.itemsCount)}`:`Groups: ${integer(value.groupsCount)} · Events: ${integer(value.eventCount)}`);
  count.id='task-count';count.setAttribute('elementtiming','task-count');document.querySelector('#task-count').replaceWith(count);
  const facets=document.querySelector('#facets');facets.replaceChildren();
  if(task==='inventory')for(const field of ['brand','category']){
    const paragraph=element('p',field[0].toUpperCase()+field.slice(1)+': ');paragraph.dataset.facet=field;
    for(const [name,count] of Object.entries(value.facets[field]).sort((a,b)=>a[0]<b[0]?-1:a[0]>b[0]?1:0)){
      const item=element('span',`${name} (${integer(count)})`);Object.assign(item.dataset,{value:name,count:String(count)});paragraph.append(item,document.createTextNode(' '));
    }facets.append(paragraph);
  }
  data=value;main.dataset.snapshot=value.snapshot;main.dataset.responseOperation=value.read_operation;main.dataset.qrlOutcome='completed';main.dataset.ready='true';
  document.querySelector('#error').textContent='';primary.disabled=busy||(task==='inventory'&&!data.items.length);secondary.disabled=busy||last===null;
}
async function refresh(){const response=await fetch('/api/task?'+new URLSearchParams({parameters:JSON.stringify(parameters)}),{cache:'no-store',headers:{'X-QRL-Operation':`${prefix}-refresh-${sequence++}`}});const value=await response.json();if(!response.ok||!value.ok){fail(value);throw new Error('Task refresh failed');}render(value);}
async function action(second){
  if(busy)return;busy=true;primary.disabled=true;secondary.disabled=true;const key=`${prefix}-${second?'secondary':'primary'}-${sequence++}`;let payload;
  if(task==='inventory')payload=second?{operation_id:key,kind:'cancel',reservation_id:last.reservation_id}:{operation_id:key,kind:'reserve',reservation_id:key,product_id:data.items[0].id,quantity:1};
  else{
    const first=data.groups[0]||{};
    payload=second?{event_id:key,kind:'refund',event_time_s:parameters.start_s,amount_minor:100,sale_id:last.event_id}:
      {event_id:key,kind:'sale',event_time_s:parameters.start_s,amount_minor:100,region:first.region||parameters.region||'region-0',category:first.category||parameters.category||'category-0'};
  }
  try{
    const response=await fetch('/api/task/mutate',{method:'POST',headers:{'Content-Type':'application/json','X-QRL-Operation':key},body:JSON.stringify(payload),cache:'no-store'}),result=await response.json();
    if(!response.ok||!result.ok)throw new Error('Action failed');
    main.dataset.lastAction=JSON.stringify({request:payload,result});document.querySelector('#action-status').textContent=result.outcome;
    if(result.changed){last=second?null:result;await refresh();}
  }catch(error){document.querySelector('#action-status').textContent='Unable to complete action. Please try again.';main.dataset.actionError=error.message;}
  finally{busy=false;const failed=main.dataset.qrlOutcome==='failed';primary.disabled=failed||(task==='inventory'&&!data?.items?.length);secondary.disabled=failed||last===null;}
}
primary.addEventListener('click',()=>action(false));secondary.addEventListener('click',()=>action(true));
if(main.dataset.strategy==='csr')refresh().catch(()=>{});else if(data?.ok===true){main.dataset.ready='true';primary.disabled=task==='inventory'&&!data.items.length;}else fail(data);
