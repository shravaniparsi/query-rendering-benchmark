"""Run separate profiling and loaded phases without changing deployment identity."""
from . import offhost_serial_design as serial,offhost_query_browser_design as query


def run_observer(output,own,peer_name,peer_ip,allocation,smoke,*,spec,cases,collected=None,probe_callback=None):
    from .offhost_serial_profile import run_observer as run_serial
    from .offhost_resources import run_observer as run_query
    if cases!=serial.cases_for(allocation,smoke)+query.cases_for(allocation,smoke):raise ValueError('phase case union differs')
    cells=[] if collected is None else collected
    remote,_=run_serial(output,own,peer_name,peer_ip,allocation,smoke,spec=spec['serial_profile_design'],cases=serial.cases_for(allocation,smoke),collected=cells,finish=False,probe_callback=probe_callback)
    loaded_remote,_=run_query(output,own,peer_name,peer_ip,allocation,smoke,spec=spec['query_browser_design'],cases=query.cases_for(allocation,smoke),collected=cells,handshake=False,probe_callback=probe_callback)
    if loaded_remote!=remote:raise ValueError('deployment identity changed between phases')
    return remote,cells


def projections(report):
    result={}
    for name,module in (('serial',serial),('query-browser',query)):
        cases=module.cases_for(report['allocation'],report['smoke']);names={c['name'] for c in cases}
        result['report-'+name+'.json']={**report,'role':module.ROLE,'design':module.design(report['smoke']),'cases':cases,
            'cells':[c for c in report['cells'] if c['name'] in names],'parent_role':report['role']}
    return result
