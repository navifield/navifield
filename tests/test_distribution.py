"""Small installed-package checks; run with python -m unittest discover -s tests."""
import importlib.resources
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class DistributionTest(unittest.TestCase):
    def test_monitor_resource_is_installed(self):
        page = importlib.resources.files('navifield').joinpath('live_monitor.html').read_text(encoding='utf-8')
        self.assertIn('运行监控', page)

    def test_default_owner_uses_local_worker(self):
        from navifield.resource_limits import ResourceOwner
        owner = ResourceOwner.__new__(ResourceOwner)
        owner.worker_module = None
        owner.cpus = {0}
        self.assertIn('navifield.local_worker', owner.worker_argv())

    def test_local_process_and_record(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'project'; case = root / 'case'; case.mkdir(parents=True)
            (case / 'fixture.py').write_text('print("fixture")\n', encoding='utf-8')
            prefix = [sys.executable, '-m', 'navifield.local_cli']
            run = subprocess.run(prefix + ['run', '--project', str(root), '--cwd', 'case', '--', sys.executable, 'fixture.py'],
                                 cwd=temp, capture_output=True, text=True, timeout=40)
            self.assertEqual(run.returncode, 0, run.stderr)
            rid = run.stdout.strip().splitlines()[0]
            wait = subprocess.run(prefix + ['wait', '--project', str(root), '--rid', rid, '--timeout', '30'],
                                  cwd=temp, capture_output=True, text=True, timeout=40)
            self.assertEqual(wait.returncode, 0, wait.stderr)
            self.assertEqual(json.loads(wait.stdout)['state'], 'completed')
            self.assertTrue((root / '.navifield/records' / (rid + '.json')).is_file())


if __name__ == '__main__':
    unittest.main()
