from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from experiments.study_design.performance_release import PerformancePermission
from experiments.heldout_tasks.performance_service import PerformanceService,SQLCalls
from experiments.heldout_tasks.performance_runtime import performance_runtime


class PerformancePermissionContracts(unittest.TestCase):
    def test_metadata_dictionary_cannot_open_the_native_service(self):
        with patch('experiments.heldout_tasks.performance_service.ReservedService.__init__') as opened:
            with self.assertRaises(ValueError):PerformanceService('unused','unused','inventory',{'status':'passed'}, {})
            opened.assert_not_called()

    def test_runtime_rejects_missing_permission_before_clock_or_fixture_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)/'not-created'
            with patch('experiments.heldout_tasks.performance_runtime.ScaleInventorySQL') as created,patch('experiments.heldout_tasks.performance_runtime.time.perf_counter_ns') as clock:
                with self.assertRaises(ValueError):
                    with performance_runtime('unused',output,'inventory',{},None,target_cpu=[0],public_origin='http://100.64.0.1:8765'):pass
                created.assert_not_called();clock.assert_not_called();self.assertFalse(output.exists())

    def test_partial_source_labels_cannot_complete_the_actual_release_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'release.json').write_text(json.dumps({'source_files':{}}))
            with patch('experiments.study_design.performance_release.verify_release',return_value={'status':'passed','artifacts':{}}):
                with self.assertRaisesRegex(ValueError,'complete frozen performance'):PerformancePermission(root,'release.json','0'*64)

    def test_native_evidence_on_a_synthetic_connection_has_separate_pool_and_sql_clocks(self):
        class Cursor:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def execute(self,*args,**kwargs):return self
            def fetchone(self):return (2,)
        class Connection:
            info=SimpleNamespace(backend_pid=123)
            def execute(self,query,parameters=None,*args,**kwargs):return 1
            def cursor(self,*args,**kwargs):return Cursor()
        class ToyPool:
            @contextmanager
            def acquire(self):yield Connection(),.25
        toy=SimpleNamespace(pool=ToyPool(),local=threading.local(),catalog={},catalog_lock=threading.Lock())
        toy.reset=lambda:setattr(toy.local,'backend',None)
        def query(connection):
            connection.execute('SELECT 1',(1,))
            with connection.cursor() as cursor:self.assertEqual(cursor.execute('SELECT 2',(2,)).fetchone(),(2,))
            return {'answer':1}
        result=PerformanceService.call(toy,query);native=toy.local.backend
        self.assertEqual(result['answer'],1);self.assertEqual(result['backend']['pool_wait_ms'],.25);self.assertEqual(native['backend_pid'],123)
        self.assertGreaterEqual(int(native['finish_monotonic_ns']),int(native['start_monotonic_ns']));self.assertGreaterEqual(native['transaction_wall_ms'],0)
        digest=hashlib.sha256(b'SELECT 1').hexdigest();second=hashlib.sha256(b'SELECT 2').hexdigest()
        self.assertEqual(toy.catalog,{digest:'SELECT 1',second:'SELECT 2'});self.assertEqual([r['sql_sha256'] for r in native['explicit_execute_calls']],[digest,second])


if __name__=='__main__':unittest.main()
