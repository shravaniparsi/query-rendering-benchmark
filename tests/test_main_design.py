from collections import defaultdict
from copy import deepcopy
import unittest
from experiments.heldout_tasks.main_design import cells,trace_for
from experiments.heldout_tasks.workload import VERSION,APPLICATIONS


def artifacts():
    rows=[];catalogs={}
    for task,app in APPLICATIONS.items():
        catalogs[app]=[q+'/'+r for q in ('correlated','batched','maintained') for r in ('ssr','csr')]
        for n in (1000,100000):
            for rate in (16,64):
                for mix in (.02,.5):rows.append({'id':f'{task}-{n}-{rate}-{mix}','application':app,'rows':n,'offered_rate':rate,'nominal_write_fraction':mix,'duration_s':20})
    return {'main_contexts':{'contexts':rows,'catalogs':catalogs},'sample_plan':{'collection_dates_utc':['2026-11-10','2026-11-11'],'qualification_dates_utc':['2026-11-09']},
        'profile_protocol':{'version':'permitted-serial-profile-v2','workload_trace_version':VERSION,'serial':{'operations_per_renderer':60,'quiet_probes_per_renderer':3},
            'overhead':{'rows':1000,'window_s':5,'pairs':3,'max_operations_per_window':50000,'minimum_context_median_ratio':.90}}}


class MainDesignContracts(unittest.TestCase):
    def test_complete_fixed_grid_and_smoke_are_distinct(self):
        value=artifacts()
        for task in APPLICATIONS:
            full=cells(value,task,'2026-11-10');small=cells(value,task,'2026-11-09',True)
            self.assertEqual([sum(r['phase']==p for r in full) for p in ('qualification','serial','loaded')],[6,12,48])
            self.assertEqual([sum(r['phase']==p for r in small) for p in ('qualification','serial','loaded')],[6,12,24]);self.assertEqual(len({r['name'] for r in full}),66)
            self.assertEqual(sum(len(trace_for(value,r)) for r in full if r['phase']=='loaded'),38400)

    def test_every_action_shares_the_same_prospective_trace_within_context(self):
        value=artifacts()
        for task in APPLICATIONS:
            panels=defaultdict(list)
            for row in cells(value,task,'2026-11-10'):
                if row['phase']=='loaded':panels[row['context_id']].append(trace_for(value,row))
            self.assertEqual(len(panels),8)
            for traces in panels.values():self.assertEqual(len(traces),6);self.assertTrue(all(t==traces[0] for t in traces))
            other=[r for r in cells(value,task,'2026-11-11') if r['phase']=='loaded'][0]
            first=next(r for r in cells(value,task,'2026-11-10') if r.get('context_id')==other['context_id'])
            self.assertNotEqual(trace_for(value,first),trace_for(value,other))

    def test_unplanned_dates_or_changed_budgets_are_rejected(self):
        value=artifacts()
        with self.assertRaises(ValueError):cells(value,'inventory','2026-11-08')
        bad=deepcopy(value);bad['profile_protocol']['serial']['operations_per_renderer']=30
        with self.assertRaises(ValueError):cells(bad,'inventory','2026-11-10')

    def test_full_qualification_keeps_both_scales_and_qualification_identity(self):
        value=artifacts();value['sample_plan']['qualification_date_mode']='complete_profile_and_loaded_full_panel'
        for task in APPLICATIONS:
            full=cells(value,task,'2026-11-09',True)
            self.assertEqual(len(full),66);self.assertTrue(all(r['smoke'] is True for r in full))
            self.assertEqual({r['rows'] for r in full if r['phase']=='loaded'},{1000,100000})
        value['sample_plan']['qualification_date_mode']='arbitrary_subset'
        with self.assertRaises(ValueError):cells(value,'inventory','2026-11-09',True)


if __name__=='__main__':unittest.main()
