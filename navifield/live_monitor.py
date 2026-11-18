"""Follow a retained local run: one blocking wait, and a read-only local monitor page."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import re
import threading
import time
from urllib.parse import parse_qs, urlparse

from . import execution, project
from .foam_readers import ResidualParser

TERMINAL = ('completed', 'failed', 'cancelled')
_RID = re.compile(r'^r[0-9]{3,}$')
_FATAL = (('FOAM FATAL', 'solver reported a fatal error'),
          ('Segmentation fault', 'segmentation fault'),
          ('job aborted', 'parallel job aborted'))
_TRAP_ENABLED = re.compile(r'^\s*(?:trapFpe|sigFpe)\s*:\s*Floating point exception trapping enabled \(FOAM_SIGFPE\)\.\s*$', re.I)
_NONFINITE = re.compile(r'residual\s*=\s*-?(?:nan|inf)', re.I)
_MAX_POINTS = 20000
_MAX_SERIES = 24


def observed_status(root, rid):
    """Run state as the local controller, its owner and the record together show it."""
    result = execution.run_status(root, rid)
    state = result.get('data', {}).get('state')
    if state not in ('queued', 'running', *TERMINAL):
        return result
    folder = project.resolve_path(root, f'.navifield/runs/{rid}')
    controller, alive = {}, False
    try:
        import psutil
        controller = json.loads((folder / 'controller.json').read_text(encoding='utf-8'))
        proc = psutil.Process(controller['pid'])
        alive = abs(proc.create_time() - controller['created']) < .001 and proc.is_running()
    except Exception:
        pass
    if state in ('queued', 'running') and not alive:
        result['data']['state'] = 'unknown'
    elif state in TERMINAL:
        try:
            owner = json.loads((folder / 'owner' / 'local' / 'owner.json').read_text(encoding='utf-8'))
            complete = (controller.get('state') == state and owner.get('phase') == 'empty' and
                        (Path(root) / '.navifield' / 'records' / (rid + '.json')).is_file())
        except (OSError, ValueError, TypeError, AttributeError):
            complete = False
        if not complete:
            result['data']['state'] = 'running' if alive else 'unknown'
    return result


class LogFollower:
    """Read only new bytes of a growing native log; keep residual series and fatal messages."""

    def __init__(self, path):
        self.path = Path(path)
        self.offset = 0
        self.parser = ResidualParser()
        self.order = []
        self.series = {}
        self.generation = 0
        self.fatal = None
        self.last_axis = None
        self._tail = ''
        self._fatal_line = ''
        self.recent = []

    def _reset(self):
        self.__init__(self.path)
        self.generation += 1

    def poll(self):
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        if size < self.offset:
            generation = self.generation
            self._reset()
            self.generation = generation + 1
        for _ in range(64):
            try:
                with self.path.open('rb') as stream:
                    stream.seek(self.offset)
                    data = stream.read(1 << 20)
            except OSError:
                return
            if not data:
                return
            self.offset += len(data)
            text = self._tail + data.decode('latin-1')
            if self.fatal is None:
                for marker, meaning in _FATAL:
                    if marker in text:
                        self.fatal = meaning
                        break
                else:
                    if _NONFINITE.search(text):
                        self.fatal = 'non-finite residual'
            complete = (self._fatal_line + data.decode('latin-1')).split('\n')
            self._fatal_line = complete.pop()[-65536:]
            for line in complete:
                self._check_float_line(line)
            lines = text.splitlines()
            self.recent = [line[:300] for line in (self.recent + lines)[-12:]]
            self._tail = text[-80:]
            for point in self.parser.feed(data):
                key = point.equation if not point.region else point.region + ':' + point.equation
                if key not in self.series:
                    if len(self.order) >= _MAX_SERIES:
                        continue
                    self.order.append(key)
                    self.series[key] = []
                values = self.series[key]
                values.append((point.axis, point.initial))
                self.last_axis = point.axis
                if len(values) > _MAX_POINTS:
                    for name in self.order:
                        self.series[name] = self.series[name][::2]
                    self.generation += 1

    def _check_float_line(self, line):
        if (self.fatal is None and 'floating point exception' in line.lower()
                and not _TRAP_ENABLED.fullmatch(line)):
            self.fatal = 'floating point exception'

    def finish(self):
        # A terminated program may end with an unterminated diagnostic.
        self._check_float_line(self._fatal_line)

    def latest(self):
        return {key: self.series[key][-1][1] for key in self.order if self.series[key]}

    def increment(self, generation=None, have=()):
        """Points the caller does not hold yet; everything when its generation is stale."""
        fresh = generation != self.generation
        points = {}
        for index, key in enumerate(self.order):
            start = 0 if fresh or index >= len(have) else max(0, have[index])
            points[key] = self.series[key][start:]
        return {'generation': self.generation, 'keys': list(self.order), 'reset': fresh, 'points': points}


def _table(path, limit=1500):
    """Numeric columns of one function-object table; the first column is the axis."""
    names, rows = None, []
    try:
        with open(path, encoding='latin-1') as stream:
            for line in stream:
                text = line.strip()
                if not text:
                    continue
                if text.startswith('#'):
                    names = text.lstrip('#').split()
                    continue
                try:
                    row = [float(token) for token in text.replace('(', ' ').replace(')', ' ').split()]
                except ValueError:
                    continue
                if row and all(math.isfinite(value) for value in row):
                    rows.append(row)
    except OSError:
        return []
    if not rows:
        return []
    width = min(len(row) for row in rows)
    if width < 2:
        return []
    stride = max(1, len(rows) // limit)
    rows = rows[::stride] + ([rows[-1]] if (len(rows) - 1) % stride else [])
    labels = names if names and len(names) == width else None
    result = []
    for column in range(1, min(width, 7)):
        label = labels[column] if labels else f'column {column + 1}'
        result.append({'label': label, 'points': [(row[0], row[column]) for row in rows]})
    return result


def case_tables(case, limit_files=12):
    """Latest function-object tables under the run's case, without solver-specific knowledge."""
    base = Path(case) / 'postProcessing'
    found = {}
    try:
        for path in sorted(base.glob('*/*/*.dat')):
            key = (path.parts[-3], path.name)
            if key not in found or path.stat().st_mtime >= found[key].stat().st_mtime:
                found[key] = path
    except OSError:
        return []
    tables = []
    for (name, filename), path in list(found.items())[:limit_files]:
        columns = _table(path)
        if columns:
            tables.append({'name': name + '/' + filename, 'columns': columns})
    return tables


def _follower(root, rid):
    run = project.read_run(root, rid)
    relative = (run.get('execution_state') or {}).get('log_path') or f'result/{rid}/native.log'
    return LogFollower(project.resolve_path(root, relative)), run, relative


def _round(value):
    return float(f'{value:.3g}')


def wait(root, rid, *, timeout=None, residual_below=None, stop_on_divergence=False, then_stop=False,
         poll_seconds=.5, sleep=time.sleep, clock=time.monotonic):
    """Block until the run ends, a fatal message appears, a residual target is met, or time runs out."""
    root = Path(root).resolve()
    follower, _, log_relative = _follower(root, rid)
    folder = project.resolve_path(root, f'.navifield/runs/{rid}')
    started = clock()
    reason, state, stop_sent = None, None, False
    while True:
        follower.poll()
        status = observed_status(root, rid)
        state = status.get('data', {}).get('state')
        if state in TERMINAL:
            follower.poll()
            follower.finish()
            reason = reason or 'finished'
            break
        if state == 'unknown' or status.get('status') != 'ok':
            reason = reason or 'state_not_observed'
            break
        if not stop_sent:
            if follower.fatal:
                reason = 'fatal_message_in_log'
                if not stop_on_divergence:
                    break
                (folder / 'stop.request').touch()
                stop_sent = True
            elif residual_below is not None:
                latest = follower.latest()
                if latest and all(value < residual_below for value in latest.values()):
                    reason = 'residual_target_met'
                    if not then_stop:
                        break
                    (folder / 'stop.request').touch()
                    stop_sent = True
        if timeout is not None and clock() - started >= timeout:
            reason = reason or 'timeout'
            break
        sleep(poll_seconds)
    run = project.read_run(root, rid)
    monitors = {}
    if run.get('execution_case_path'):
        for table in case_tables(project.resolve_path(root, run['execution_case_path'])):
            monitors[table['name']] = {column['label']: {'axis': column['points'][-1][0],
                                                       'value': column['points'][-1][1]}
                                       for column in table['columns']}
    summary = {'rid': rid, 'state': state, 'reason': reason, 'waited_seconds': round(clock() - started, 1),
               'exit_code': (run.get('execution_state') or {}).get('exit_code'),
               'last_step': follower.last_axis,
               'residuals': {key: _round(value) for key, value in follower.latest().items()},
               'fatal': follower.fatal, 'stop_requested': stop_sent, 'log': log_relative,
               'monitors': monitors}
    return summary


def wait_exit_code(summary):
    """0 completed; 3 still running when the wait returned; 1 anything else."""
    if summary['state'] == 'completed' and not summary['fatal']:
        return 0
    if summary['state'] in ('queued', 'running'):
        return 3
    return 1


class _Monitor:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.lock = threading.Lock()
        self.followers = {}

    def runs(self):
        rows = []
        for manifest in sorted((self.root / '.navifield' / 'runs').glob('r*/manifest.json')):
            try:
                run = json.loads(manifest.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            if not isinstance(run, dict):
                continue
            rid = run.get('run_id')
            if not isinstance(rid, str) or not _RID.fullmatch(rid) or rid != manifest.parent.name:
                continue
            state = run.get('execution_state') or {}
            request = run.get('execution_request') or {}
            if not isinstance(state, dict) or not isinstance(request, dict):
                continue
            rows.append({'rid': run.get('run_id'), 'state': state.get('state'),
                         'program': request.get('program'), 'started_at': state.get('started_at')})
        return rows

    def run(self, rid, generation, have):
        with self.lock:
            if rid not in self.followers:
                self.followers[rid] = _follower(self.root, rid)
            follower, _, log_relative = self.followers[rid]
            follower.poll()
            if observed_status(self.root, rid).get('data', {}).get('state') in TERMINAL:
                follower.finish()
            residuals = follower.increment(generation, have)
            fatal, recent, last = follower.fatal, list(follower.recent), follower.last_axis
        status = observed_status(self.root, rid)
        run = project.read_run(self.root, rid)
        state = run.get('execution_state') or {}
        tables = []
        if run.get('execution_case_path'):
            tables = case_tables(project.resolve_path(self.root, run['execution_case_path']))
        return {'rid': rid, 'state': status.get('data', {}).get('state'), 'fatal': fatal, 'last_step': last,
                'program': (run.get('execution_request') or {}).get('program'),
                'started_at': state.get('started_at'), 'finished_at': state.get('finished_at'),
                'exit_code': state.get('exit_code'), 'log': log_relative, 'recent': recent,
                'residuals': residuals, 'tables': tables}


def _handler(monitor, page):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body, kind):
            self.send_response(code)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            host = (self.headers.get('Host') or '').rsplit(':', 1)[0]
            if host not in ('127.0.0.1', 'localhost'):
                return self._send(403, b'local access only', 'text/plain')
            url = urlparse(self.path)
            query = parse_qs(url.query)
            try:
                if url.path == '/':
                    return self._send(200, page, 'text/html; charset=utf-8')
                if url.path == '/api/runs':
                    body = {'project': monitor.root.name, 'runs': monitor.runs()}
                elif url.path == '/api/run':
                    rid = query.get('rid', [''])[0]
                    if not _RID.fullmatch(rid):
                        return self._send(400, b'invalid run id', 'text/plain')
                    generation = int(query['gen'][0]) if query.get('gen') else None
                    have = [int(item) for item in query.get('have', [''])[0].split(',') if item]
                    body = monitor.run(rid, generation, have)
                else:
                    return self._send(404, b'not found', 'text/plain')
            except (OSError, ValueError, KeyError, TypeError) as error:
                return self._send(404, str(error).encode('utf-8', 'replace')[:300], 'text/plain')
            self._send(200, json.dumps(body, allow_nan=False).encode('utf-8'), 'application/json')
    return Handler


def serve(root, *, port=8790, announce=print):
    """Serve the read-only page on the loopback interface until interrupted."""
    page = (Path(__file__).with_name('live_monitor.html')).read_bytes()
    monitor = _Monitor(root)
    try:
        server = ThreadingHTTPServer(('127.0.0.1', port), _handler(monitor, page))
    except OSError:
        server = ThreadingHTTPServer(('127.0.0.1', 0), _handler(monitor, page))
    server.daemon_threads = True
    announce(json.dumps({'monitor': f'http://127.0.0.1:{server.server_address[1]}/', 'project': str(monitor.root),
                         'stop': 'Ctrl+C'}), flush=True)
    try:
        server.serve_forever(poll_interval=.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
