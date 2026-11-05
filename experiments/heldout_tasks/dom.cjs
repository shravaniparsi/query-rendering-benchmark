// Materialized visible text and structured task state.
async function capture(page,task){
  return page.evaluate(task=>{
    const main=document.querySelector('main'),parameters=JSON.parse(document.querySelector('#parameters').textContent);
    const value={read_operation:main.dataset.responseOperation,snapshot:main.dataset.snapshot,parameters};
    if(task==='inventory'){
      value.itemsCount=Number(document.querySelector('#task-count').textContent.replace('Products: ',''));
      value.items=Array.from(document.querySelectorAll('article'),row=>({id:Number(row.dataset.id),category:row.dataset.category,brand:row.dataset.brand,
        price_minor:Number(row.dataset.priceMinor),initial_stock:Number(row.dataset.initialStock),remaining_stock:Number(row.dataset.remainingStock)}));
      value.facets={};for(const name of ['category','brand'])value.facets[name]=Object.fromEntries(Array.from(document.querySelectorAll(`[data-facet="${name}"] span`),node=>[node.dataset.value,Number(node.dataset.count)]));
    }else{
      value.parameters={start_s:parameters.start_s,end_s:parameters.end_s,group_by:parameters.group_by||['region','category'],
        region:parameters.region??null,category:parameters.category??null,offset:parameters.offset??0,limit:parameters.limit??20};
      const match=document.querySelector('#task-count').textContent.match(/^Groups: (\d+) · Events: (\d+)$/);if(!match)throw new Error('Visible analytics count differs');
      value.groupsCount=Number(match[1]);value.eventCount=Number(match[2]);const keys=parameters.group_by||['region','category'];
      value.groups=Array.from(document.querySelectorAll('article'),row=>{
        const result=Object.fromEntries(keys.map(key=>[key,row.dataset[key]]));
        for(const key of ['gross_total_minor','refund_total_minor','sales_count','refund_count','net_total_minor'])result[key]=Number(row.dataset[key.replace(/_([a-z])/g,(_,c)=>c.toUpperCase())]);return result;
      });const window=document.querySelector('#window-label');value.window={start_s:Number(window.dataset.startS),end_s:Number(window.dataset.endS),bounds:'[start,end)'};
    }
    const count=document.querySelector('#task-count').textContent,articles=Array.from(document.querySelectorAll('article'));
    const money=n=>(n<0?'-':'')+'$'+Math.floor(Math.abs(n)/100)+'.'+String(Math.abs(n)%100).padStart(2,'0');
    const titles=articles.map(row=>row.querySelector('h2').textContent),values=articles.map(row=>row.querySelector('output').textContent);
    const expectedTitles=(task==='inventory'?value.items:value.groups).map(row=>task==='inventory'?`Product ${row.id}`:value.parameters.group_by.map(key=>row[key]).join(' / '));
    const expectedValues=(task==='inventory'?value.items:value.groups).map(row=>task==='inventory'?`Available: ${row.remaining_stock}`:`Net sales: ${money(row.net_total_minor)}`);
    if(JSON.stringify(titles)!==JSON.stringify(expectedTitles)||JSON.stringify(values)!==JSON.stringify(expectedValues))throw new Error('Actual visible title/value differs');
    value.visible_text={count,titles,values};
    if(task==='inventory'){
      const prices=articles.map(row=>row.querySelector('[data-price]').textContent);
      if(JSON.stringify(prices)!==JSON.stringify(value.items.map(row=>`Price: ${money(row.price_minor)}`)))throw new Error('Actual visible price differs');
      const facetLabels={};for(const field of ['brand','category'])facetLabels[field]=Array.from(document.querySelectorAll(`[data-facet="${field}"] span`),node=>({value:node.dataset.value,text:node.textContent}));
      for(const field of ['brand','category'])for(const row of facetLabels[field])if(row.text!==`${row.value} (${value.facets[field][row.value]})`)throw new Error('Actual visible facet differs');
      Object.assign(value.visible_text,{prices,facet_labels:facetLabels});
    }else{
      const windowText=document.querySelector('#window-label').textContent;
      if(windowText!==`Time window: ${value.window.start_s} inclusive to ${value.window.end_s} exclusive`)throw new Error('Actual visible time window differs');
      value.visible_text.window=windowText;
    }
    return value;
  },task);
}

module.exports={capture};
