"""Shared monitor-table evaluation for local and service execution."""
import csv
import math
from . import project


def _monitors(root, specs, *, table_lines=None, sample_counts=None):
    result = {}
    for spec in specs:
        name = spec['name']
        if spec['kind'] != 'table':
            continue
        item = {'name': name, 'status': 'unavailable', 'value': None, 'unit': spec['unit'], 'sample_count': 0}
        try:
            lines = (table_lines[name] if table_lines is not None else
                     project.resolve_path(root, spec['path']).read_text(encoding='utf8').splitlines())
            rows = [row for row in lines if row.strip() and not row.lstrip().startswith('#')]
            if spec['format'] not in ('csv', 'whitespace'):
                raise ValueError('Unsupported table format.')
            rows = list(csv.reader(rows)) if spec['format'] == 'csv' else [row.split() for row in rows]
            values = [float(row[spec['value_column']]) for row in rows]
            if not values or not all(math.isfinite(x) for x in values):
                raise ValueError('No finite samples.')
            item.update(value=values[-1], sample_count=(sample_counts[name] if sample_counts is not None else len(values)), reason='No criteria supplied.')
            if spec.get('time_column') is not None:
                observed_time = float(rows[-1][spec['time_column']])
                if not math.isfinite(observed_time):
                    item['value'] = None
                    raise ValueError('Monitor time is not finite.')
                item['time'] = observed_time
            criteria = spec.get('criteria') or {}
            n = criteria.get('window_samples')
            if n:
                if len(values) < n or n < 2:
                    raise ValueError('Insufficient window samples.')
                window = values[-n:]; mean = sum(window)/n
                if mean == 0:
                    raise ValueError('Window mean is zero.')
                metrics = {'relative_fluctuation_max': (max(window)-min(window))/abs(mean),
                           'relative_drift_max': abs(window[-1]-window[0])/abs(mean)}
                if 'relative_window_mean_difference_max' in criteria:
                    if len(values) < 2*n:
                        raise ValueError('Insufficient samples for previous window.')
                    previous = sum(values[-2*n:-n])/n
                    if previous == 0:
                        raise ValueError('Previous window mean is zero.')
                    metrics['relative_window_mean_difference_max'] = abs(mean-previous)/abs(previous)
                selected = {key: metrics[key] for key in metrics if key in criteria}
                if selected:
                    item.update(status='met' if all(value <= criteria[key] for key, value in selected.items()) else 'not_met',
                                metrics=selected, reason=None)
        except (OSError, ValueError, KeyError, IndexError, TypeError) as error:
            item['reason'] = str(error)
        result[name] = item
    for spec in specs:
        if spec['kind'] != 'difference':
            continue
        item = {'name': spec['name'], 'status': 'unavailable', 'value': None, 'unit': None, 'sample_count': 0}
        try:
            left, right = result[spec['left']], result[spec['right']]
            if left['value'] is None or right['value'] is None or left['unit'] != right['unit']:
                raise ValueError('Inputs unavailable or units differ.')
            if 'time' in left or 'time' in right:
                if 'time' not in left or 'time' not in right or left['time'] != right['time']:
                    raise ValueError('Monitor times differ or one time is unavailable.')
                item['alignment'] = 'time'
            else:
                if left['sample_count'] != right['sample_count']:
                    raise ValueError('Monitor sample counts differ; row alignment unavailable.')
                item['alignment'] = 'row_index'
            a, b = left['value'], right['value']
            delta = a + b if spec['operation'] == 'signed_sum' else a - b
            relative = None if max(abs(a), abs(b)) == 0 else abs(delta)/max(abs(a), abs(b))
            item.update(value=delta, unit=left['unit'], sample_count=min(left['sample_count'], right['sample_count']),
                        relative=relative)
            if item['alignment'] == 'row_index':
                item.update(status='unavailable', reason='Time alignment has not been verified.')
            else:
                criteria = spec['criteria']; checks = []
                if 'absolute_max' in criteria: checks.append(abs(delta) <= criteria['absolute_max'])
                if 'relative_max' in criteria:
                    if relative is None: raise ValueError('Relative denominator is zero.')
                    checks.append(relative <= criteria['relative_max'])
                item.update(status=('met' if all(checks) else 'not_met') if checks else 'unavailable', reason=None)
        except (ValueError, KeyError, TypeError) as error:
            item['reason'] = str(error)
        result[item['name']] = item
    return list(result.values())
