from pathlib import Path
import subprocess
import sys
import unittest


class BrowserEntrypointContracts(unittest.TestCase):
    def test_actual_spawned_cpuset_helper_loads_from_the_package(self):
        root=Path(__file__).resolve().parents[1]
        result=subprocess.run([sys.executable,str(root/'experiments/query_rendering_load/cpu_exec.py'),'--help'],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        for option in ('--cpu','--uid','--gid'):self.assertIn(option,result.stdout)


if __name__=='__main__':unittest.main()
