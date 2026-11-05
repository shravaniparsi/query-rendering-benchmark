"""Serial profiles followed by matched loaded panels on the same runner pair."""
from . import offhost_query_browser_design as query,offhost_serial_design as serial
ROLE='off-host deployment-matched profiles and all-query browser development'


def design(smoke=False):
    q=query.design(smoke);s=serial.design(smoke)
    return {**q,'query_browser_design':q,'serial_profile_design':s,'target_deadline_s':2700,'target_idle_s':600,
            'scope':'Serial profiling precedes loaded evaluation on one paired deployment; every case uses a fresh fixture. Development only.'}


def cases_for(allocation,smoke=False):return serial.cases_for(allocation,smoke)+query.cases_for(allocation,smoke)
