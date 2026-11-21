"""Collect end-of-run values of several retained runs into one table."""
from __future__ import annotations

import csv
import io
import json
import math
import os
from pathlib import Path
import statistics
import tempfile

from . import project
from .live_monitor import LogFollower, observed_status, run_tables


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _run_values(root, rid, window):
    """Observed values of one run, keyed by a stable quantity name; nothing is inferred."""
    run = project.read_run(root, rid)
    state = run.get('execution_state') or {}
    values = {'state': observed_status(root, rid).get('data', {}).get('state'),
              'exit_code': state.get('exit_code')}
    try:
        record = json.loads((Path(root) / '.navifield' / 'records' / (rid + '.json')).read_text(encoding='utf-8'))
        resources = record.get('resources') or {}
        values.update(cores=resources.get('cores'), wall_seconds=resources.get('wall_seconds'))
    except (OSError, ValueError):
        pass
    follower = LogFollower(project.resolve_path(root, state.get('log_path') or f'result/{rid}/native.log'))
    follower.poll()
    follower.finish()
    values['last_step'] = follower.last_axis
    for key, value in follower.latest().items():
        values['residual:' + key] = value
    tables, source = run_tables(root, run, limit=10 ** 9)
    case = run.get('execution_case_path') if source == 'case' else None
    values['monitor_tables_from'] = {'run': 'kept with this run', 'run_partial': 'run copy incomplete',
                                    'run_unavailable': 'run copy unavailable', 'case': 'case folder as it is now'}.get(source)
    if (run.get('monitor_tables') or {}).get('skipped'):
        values['monitor_tables_skipped'] = json.dumps(run['monitor_tables']['skipped'], ensure_ascii=False)
    if tables:
        for table in tables:
            for column in table['columns']:
                points = column['points']
                name = f"{table['name']}:{column['label']}"
                tail = [value for _, value in points[-window:]]
                values[name + ' [last]'] = points[-1][1]
                values[name + ' [at]'] = points[-1][0]
                if len(tail) > 1:
                    mean = statistics.fmean(tail)
                    values[name + f' [mean of last {len(tail)}]'] = mean
                    values[name + f' [range of last {len(tail)}]'] = max(tail) - min(tail)
    return values, case


def collect(root, rids, *, baseline=None, labels=None, window=10):
    """Rows of quantity -> value per run, with differences from a baseline run when one is named."""
    root = Path(root).resolve()
    labels = labels or {}
    if len(set(rids)) != len(rids):
        raise ValueError('Each run may be listed once.')
    if baseline is not None and baseline not in rids:
        raise ValueError('The baseline must be one of the listed runs.')
    per_run, cases = {}, {}
    for rid in rids:
        per_run[rid], case = _run_values(root, rid, window)
        cases.setdefault(case, []).append(rid)
    shared = [sorted(group) for case, group in cases.items() if case and len(group) > 1]
    quantities = []
    for rid in rids:
        for key in per_run[rid]:
            if key not in quantities:
                quantities.append(key)
    rows = []
    for key in quantities:
        row = {'quantity': key, 'values': {rid: per_run[rid].get(key) for rid in rids}}
        if baseline is not None and key not in ('state', 'exit_code', 'monitor_tables_from'):
            reference = _number(per_run[baseline].get(key))
            row['relative_to_baseline'] = {}
            for rid in rids:
                value = _number(per_run[rid].get(key))
                if rid != baseline and value is not None and reference not in (None, 0):
                    row['relative_to_baseline'][rid] = (value - reference) / abs(reference)
        rows.append(row)
    return {'runs': [{'rid': rid, 'label': labels.get(rid)} for rid in rids], 'baseline': baseline,
            'window_samples': window, 'rows': rows, 'runs_sharing_a_case': shared,
            'note': ('These runs have no monitor tables kept with them, so their tables are read from the case '
                     'folder as it is now. Runs that share a case folder show the same tables; those values '
                     'cannot be attributed to one of them.' if shared else None)}


def _atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8-sig' if path.suffix == '.csv' else 'utf-8',
                                         newline='', dir=path.parent, suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write(root, table, output='report/run-table.csv'):
    """Write the table as CSV and the complete result as JSON beside it; return project-relative paths."""
    root = Path(root).resolve()
    target = project.resolve_path(root, output)
    if target.suffix.lower() != '.csv':
        raise ValueError('The output must be a project-relative .csv path.')
    rids = [run['rid'] for run in table['runs']]
    header = ['quantity'] + [rid if not run['label'] else f"{rid} ({run['label']})"
                             for rid, run in zip(rids, table['runs'])]
    baseline = table['baseline']
    others = [rid for rid in rids if rid != baseline] if baseline else []
    header += [f'{rid} vs {baseline} [%]' for rid in others]
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator='\n')
    writer.writerow(header)
    for row in table['rows']:
        cells = [row['quantity']] + ['' if row['values'][rid] is None else row['values'][rid] for rid in rids]
        for rid in others:
            value = (row.get('relative_to_baseline') or {}).get(rid)
            cells.append('' if value is None else f'{100 * value:.4g}')
        writer.writerow(cells)
    _atomic(target, buffer.getvalue())
    detail = target.with_suffix('.json')
    _atomic(detail, json.dumps(table, ensure_ascii=False, indent=2))
    return target.relative_to(root).as_posix(), detail.relative_to(root).as_posix()


def preview(table, csv_path, json_path, limit=6):
    """A short result for the terminal: counts, paths and the first few monitor quantities."""
    rids = [run['rid'] for run in table['runs']]
    chosen = [row for row in table['rows'] if row['quantity'].endswith('[last]')][:limit]
    sample = {}
    for row in chosen:
        entry = {rid: (float(f'{value:.5g}') if _number(value) is not None else value)
                 for rid, value in row['values'].items()}
        relative = row.get('relative_to_baseline') or {}
        if relative:
            entry['vs_baseline_percent'] = {rid: float(f'{100 * value:.3g}') for rid, value in relative.items()}
        sample[row['quantity'][:100]] = entry
    return {'runs': rids, 'states': next((row['values'] for row in table['rows'] if row['quantity'] == 'state'), {}),
            'quantities': len(table['rows']), 'baseline': table['baseline'], 'preview': sample,
            'runs_sharing_a_case': table['runs_sharing_a_case'], 'csv': csv_path, 'details': json_path}
