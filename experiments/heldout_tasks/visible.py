"""Independent expected visible labels for collected reserved DOM snapshots."""
from .inventory import require


def money(value):
    require(type(value) is int,'integer currency required');magnitude=abs(value)
    return ('-' if value<0 else '')+'$'+str(magnitude//100)+'.'+str(magnitude%100).zfill(2)


def verify_visible(evidence):
    actual=evidence['visible_text']
    if evidence['read_operation']=='inventory_page':
        rows=evidence['items'];expected={'count':'Products: '+str(evidence['itemsCount']),
            'titles':['Product '+str(r['id']) for r in rows],'values':['Available: '+str(r['remaining_stock']) for r in rows],
            'prices':['Price: '+money(r['price_minor']) for r in rows]}
        require(set(actual)==set(expected)|{'facet_labels'},'inventory visible label coverage differs')
        for field in ('brand','category'):
            labels=actual['facet_labels'][field];mapping={r['value']:r['text'] for r in labels}
            require(len(mapping)==len(labels) and mapping=={name:f'{name} ({count})' for name,count in evidence['facets'][field].items()},'actual facet label differs')
        require(set(actual['facet_labels'])=={'brand','category'},'visible facet fields differ')
    else:
        require(evidence['read_operation']=='event_time_groups','unknown visible operation');rows=evidence['groups'];keys=evidence['parameters']['group_by'];window=evidence['window']
        expected={'count':f"Groups: {evidence['groupsCount']} · Events: {evidence['eventCount']}",
            'titles':[' / '.join(r[key] for key in keys) for r in rows],'values':['Net sales: '+money(r['net_total_minor']) for r in rows],
            'window':f"Time window: {window['start_s']} inclusive to {window['end_s']} exclusive"}
        require(set(actual)==set(expected),'analytics visible label coverage differs')
    require(all(actual[k]==v for k,v in expected.items()),'actual visible labels differ from semantic snapshot');return True


def verify_failure(value):
    require(value=={'text':'Unable to load. Please try again.','outcome':'failed','error':'origin_http_failure','upstream_status':'503',
        'articles':0,'primary_disabled':True,'secondary_disabled':True},'actual visible failure state differs');return True


def verify_paint(evidence,markers):
    text=evidence['visible_text'];expected={'task-count':text['count'],'task-first-title':text['titles'][0],'task-first-value':text['values'][0]}
    require(isinstance(markers,list) and len(markers)==3 and all(set(row)=={'identifier','text','visible'} and row['visible'] is True for row in markers),'complete untimed visible paint markers required')
    actual={row['identifier']:row['text'] for row in markers};require(len(actual)==3 and actual==expected,'paint marker content differs from actual visible task');return True
