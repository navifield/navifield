"""Optional per-run stage records for scripts launched by the local CLI."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
import time
import warnings


def _now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


class StageProgress:
    def __init__(self):
        path = os.environ.get('NAVIFIELD_STAGE_FILE')
        self.path = Path(path) if path else None
        self.lock = threading.Lock()
        self.stages = []
        self.warned = False

    def _write(self):
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(self.path.name + '.tmp')
            temporary.write_text(json.dumps({'version': 1, 'stages': self.stages}, ensure_ascii=False), encoding='utf-8')
            os.replace(temporary, self.path)
        except OSError as error:
            if not self.warned:
                warnings.warn(f'stage progress is unavailable: {error}', RuntimeWarning)
                self.warned = True

    @contextmanager
    def step(self, name):
        record = {'name': str(name), 'state': 'running', 'started_at': _now(),
                  'ended_at': None, 'elapsed_seconds': None, 'exit_code': None}
        with self.lock:
            self.stages.append(record)
            self._write()
        start = time.monotonic()
        try:
            yield record
        except BaseException as error:
            with self.lock:
                record.update(state='failed', ended_at=_now(), elapsed_seconds=time.monotonic()-start,
                              exit_code=getattr(error, 'returncode', record['exit_code']))
                self._write()
            raise
        else:
            with self.lock:
                record.update(state='completed' if record['exit_code'] in (None, 0) else 'failed',
                              ended_at=_now(), elapsed_seconds=time.monotonic()-start,
                              exit_code=0 if record['exit_code'] is None else record['exit_code'])
                self._write()
