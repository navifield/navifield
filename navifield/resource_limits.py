"""Local CLI worker selection over the shared OS resource owner."""
import os
from pathlib import Path
import sys
from .resource_owner_core import ResourceOwner as _ResourceOwner


class ResourceOwner(_ResourceOwner):
    def worker_argv(self):
        module = self.worker_module or __package__ + '.local_worker'
        if os.name == 'nt':
            return [sys.executable, '-B', '-m', module]
        bootstrap = Path(__file__).parent / 'worker_bootstrap.py'
        return [sys.executable, '-S', '-B', str(bootstrap), ','.join(map(str, sorted(self.cpus))), module]
