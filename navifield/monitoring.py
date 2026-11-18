"""Incremental per-run table observations, shared by local and target workers."""
from collections import deque
from pathlib import Path
import os


class LiveTables:
    """Read only bytes produced after construction, retaining bounded complete rows."""
    def __init__(self, specs, paths, evaluate):
        self.specs = specs
        self.evaluate = evaluate
        self.tables = {}
        for spec in specs:
            if spec['kind'] != 'table':
                continue
            path = Path(paths[spec['name']])
            n = (spec.get('criteria') or {}).get('window_samples', 2)
            state = {'path': path, 'identity': None, 'offset': 0, 'partial': b'',
                     'skip_partial': False, 'count': 0, 'anchor': b'',
                     'lines': deque(maxlen=max(2, 2 * n))}
            try:
                with path.open('rb') as stream:
                    stat = stream.fileno()
                    info = os.fstat(stat)
                    state['identity'] = (info.st_dev, info.st_ino)
                    state['offset'] = info.st_size
                    if info.st_size:
                        stream.seek(max(0, info.st_size - 4096))
                        state['anchor'] = stream.read(min(info.st_size, 4096))
                        stream.seek(-1, 2)
                        state['skip_partial'] = stream.read(1) != b'\n'
            except FileNotFoundError:
                pass
            self.tables[spec['name']] = state

    def sample(self):
        fresh, lagging = {}, {}
        for name, state in self.tables.items():
            fresh[name] = False
            lagging[name] = False
            try:
                with state['path'].open('rb') as stream:
                    info = os.fstat(stream.fileno())
                    identity = (info.st_dev, info.st_ino)
                    changed = state['identity'] != identity or info.st_size < state['offset']
                    if not changed and state['anchor']:
                        stream.seek(state['offset'] - len(state['anchor']))
                        changed = stream.read(len(state['anchor'])) != state['anchor']
                    if changed:
                        state.update(identity=identity, offset=0, partial=b'', skip_partial=False, count=0, anchor=b'')
                        state['lines'].clear()
                    stream.seek(state['offset'])
                    data = stream.read(65536)
                    state['offset'] = stream.tell()
                    lagging[name] = state['offset'] < os.fstat(stream.fileno()).st_size
                    stream.seek(max(0, state['offset'] - 4096))
                    state['anchor'] = stream.read(min(state['offset'], 4096))
                data = state['partial'] + data
                parts = data.split(b'\n')
                state['partial'] = parts.pop()
                if state['skip_partial'] and parts:
                    parts.pop(0)
                    state['skip_partial'] = False
                for line in parts:
                    text = line.decode('utf8')
                    if text.strip() and not text.lstrip().startswith('#'):
                        state['lines'].append(text)
                        state['count'] += 1
                        fresh[name] = True
            except (OSError, UnicodeError):
                state['lines'].clear()
        observations = self.evaluate({name: list(item['lines']) for name, item in self.tables.items()},
                                     {name: item['count'] for name, item in self.tables.items()})
        for row in observations:
            spec = next(item for item in self.specs if item['name'] == row['name'])
            names = [row['name']] if spec['kind'] == 'table' else [spec['left'], spec['right']]
            if any(lagging.get(name, False) for name in names):
                row.update(status='unavailable', value=None,
                           reason='Unread new data remains; current table tail has not been observed.')
                row.pop('metrics', None)
                row.pop('relative', None)
            elif not all(fresh.get(name, False) for name in names):
                row.update(status='unavailable', reason='No complete new sample in this interval.')
        return observations

class CriterionStop:
    """One durable request; delivery and native acknowledgement remain distinct."""
    def __init__(self, tables, names, trigger, trigger_relative, now, persist):
        self.tables, self.names, self.trigger = tables, names, Path(trigger)
        self.now, self.persist = now, persist
        self.state = {'requested': False, 'reason': None, 'requested_at': None,
                      'trigger_path': trigger_relative, 'delivery': 'not_requested',
                      'confirmed': False, 'observed_at': None,
                      'monitor_names': list(names), 'observations': []}
        self.identity = None
        self.ack = False
        self.delivery_error = None

    def sample(self):
        rows = self.tables.sample()
        if self.state['requested']:
            self.deliver()
            return rows
        selected = {row['name']: row for row in rows}
        if all(selected[name]['status'] == 'met' for name in self.names):
            self.state.update(requested=True, reason='criteria_met', requested_at=self.now(),
                              delivery='pending', observations=[selected[name] for name in self.names])
            self.persist(self.state)
            self.deliver()
        return rows

    def deliver(self):
        if self.ack or self.state['delivery'] == 'failed':
            return
        try:
            if self.trigger.exists():
                info = self.trigger.stat()
                if self.identity != (info.st_dev, info.st_ino):
                    raise FileExistsError('Trigger path is occupied by a file not owned by this run.')
                return
            fd = os.open(self.trigger, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                info = os.fstat(fd)
                self.identity = (info.st_dev, info.st_ino)
            finally:
                os.close(fd)
            self.state['delivery'] = 'written'
        except OSError as error:
            self.state['delivery'] = 'failed'
            self.delivery_error = str(error)
        self.persist(self.state)

    def observe_line(self, raw):
        import re
        if (self.state['requested'] and self.state['delivery'] == 'written'
                and re.fullmatch(rb'USER REQUESTED ABORT \(timeIndex=[0-9]+\): stop and write data\r?\n', raw)):
            self.ack = True
            self.state['observed_at'] = self.now()
            self.persist(self.state)

    def finish(self, code, checkpoint):
        if self.state['requested']:
            self.state['confirmed'] = bool(self.ack and code == 0 and checkpoint)
            if not self.state['confirmed']:
                self.state['confirmed'] = None
        self.persist(self.state)


def field_checkpoint(case, baseline=None, *, selected=None):
    """Observe a changed, complete serial field set; never claim restart validity."""
    import math
    from .foam_readers import complete_internal_field, field_header
    def valid_field(field):
        with field.open('rb') as stream:
            prefix = stream.read(65536)
        values, dimensions = field_header(prefix)
        if (not values.get('class', '').endswith('Field') or values.get('object') != field.name
                or values.get('format') != 'ascii' or dimensions is None):
            return False
        return complete_internal_field(field, values['class'])
    case = Path(case)
    initial = case / '0'
    if not initial.is_dir() or initial.is_symlink():
        return None
    required = []
    for field in initial.rglob('*'):
        if field.is_symlink():
            return None
        if not field.is_file():
            continue
        try:
            if field.suffix == '.gz':
                return None
            with field.open('rb') as stream:
                values, _ = field_header(stream.read(65536))
            if valid_field(field):
                required.append(field.relative_to(initial))
            elif values.get('class', '').endswith('Field'):
                return None
        except (OSError, ValueError, UnicodeError):
            return None
    if not required:
        return None
    found = []
    for directory in case.iterdir():
        if selected is not None and directory.name != selected:
            continue
        if directory.is_symlink() or not directory.is_dir():
            continue
        try:
            value = float(directory.name)
        except ValueError:
            continue
        if not math.isfinite(value) or value <= 0:
            continue
        signatures = {}
        for rel in required:
            path = directory / rel
            if any(part.is_symlink() for part in (path, *path.parents) if part != directory and part.is_relative_to(directory)):
                break
            try:
                if not path.is_file() or not path.resolve().is_relative_to(directory.resolve()):
                    break
                if not valid_field(path):
                    break
                st = path.stat()
                signatures[rel.as_posix()] = (st.st_size, st.st_mtime_ns)
            except (OSError, ValueError):
                break
        else:
            if baseline is None or all(baseline.get((directory.name, key)) != signature for key, signature in signatures.items()):
                found.append((value, directory.name, signatures))
    return max(found, default=None)


def field_baseline(case):
    """Capture signatures of all existing complete candidate field sets."""
    baseline = {}
    # Include each existing candidate, not just the latest one.
    for directory in Path(case).iterdir():
        if directory.is_symlink() or not directory.is_dir() or directory.name == '0':
            continue
        try:
            float(directory.name)
        except ValueError:
            continue
        for field in directory.rglob('*'):
            if field.is_file() and not field.is_symlink():
                stat = field.stat()
                baseline[(directory.name, field.relative_to(directory).as_posix())] = (stat.st_size, stat.st_mtime_ns)
    return baseline
