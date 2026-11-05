from copy import deepcopy
import json
import unittest

from scripts.fit_endtoend_development import fit_artifact, hindsight, panel_inputs
from test_feasibility_learner import synthetic_rows


class DevelopmentAnalysisContracts(unittest.TestCase):
    def test_hindsight_separates_rendering_and_query_opportunity(self):
        configs = ['fast/ssr','fast/csr','slow/ssr','slow/csr']; catalogs = {'dev':configs}; applications = {'a':'dev','b':'dev'}
        def panel(first,second):
            return {c:{action:[action==chosen]*27 for action in configs} for c,chosen in [('a',first),('b',second)]}
        result=hindsight(panel('fast/ssr','fast/csr'),applications,catalogs)
        self.assertEqual(result['rendering_adaptation_opportunity'],.5);self.assertEqual(result['query_adaptation_opportunity'],0)
        result=hindsight(panel('fast/ssr','slow/ssr'),applications,catalogs)
        self.assertEqual(result['rendering_adaptation_opportunity'],0);self.assertEqual(result['query_adaptation_opportunity'],.5)

    def test_grouped_diagnostics_never_fit_the_held_deployment_labels(self):
        base,registry=synthetic_rows(False); rows=[];attempts=[]
        for allocation in range(3):
            for block in range(3):
                run=f'{allocation}-{block}';attempts.append({'run_id':run,'usable':True})
                for source in base:
                    row=deepcopy(source);row.update(run_id=run,allocation=allocation,block=block,context=run+'-'+row['context'],feasible=allocation==0,collection_date_utc='2026-11-05');rows.append(row)
        training={'version':'development-training-rows-v1','rows':rows,'attempt_registry':attempts}
        model,report=fit_artifact(training,json.dumps(training).encode(),registry)
        self.assertEqual(len(report['grouped_diagnostics']),6)
        held=next(f for f in report['grouped_diagnostics'] if f['group']=='allocation' and f['held_level']==0)
        self.assertEqual(held['fit']['positive_labels'],0)
        self.assertTrue(set(held['training_runs']).isdisjoint(held['evaluation_runs']))
        self.assertFalse(report['confirmatory_inference']);self.assertEqual(report['collection_days'],1)

    def test_scenario_cannot_change_its_profile_or_omit_an_outcome(self):
        rows,_=synthetic_rows();bad=deepcopy(rows);bad[1]['profile']['features']['read_service_median_ms']+=1
        with self.assertRaises(ValueError):panel_inputs(bad)
        with self.assertRaises(ValueError):panel_inputs(rows[:-1])


if __name__=='__main__':unittest.main()
