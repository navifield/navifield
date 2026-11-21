"""Portable terminal records and reference usage; never a run admission gate."""
from __future__ import annotations

import json
import math
import os
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from . import project


def _warning():
    return {'code': 'MEASUREMENT_UNAVAILABLE', 'severity': 'warn', 'measured': None, 'limit': None,
            'message': 'The portable run record could not be written; the run outcome is unchanged.',
            'location': None}


def _seconds(started, finished):
    if not isinstance(started, str) or not isinstance(finished, str):
        return None
    try:
        first = datetime.fromisoformat(started.replace('Z', '+00:00'))
        last = datetime.fromisoformat(finished.replace('Z', '+00:00'))
        value = (last - first).total_seconds()
    except (ValueError, TypeError, OverflowError):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def _relative(root, value):
    if not isinstance(value, str):
        return None
    try:
        project.resolve_path(root, value)
        return value
    except (OSError, ValueError, TypeError):
        return None


def _portable_metadata(root, value, key=''):
    """Keep descriptive values while ensuring explicit path fields remain relative."""
    if isinstance(value, dict):
        return {name: _portable_metadata(root, item, name) for name, item in value.items()}
    if isinstance(value, list):
        if key.endswith('_paths'):
            return [path for item in value if (path := _relative(root, item)) is not None]
        return [_portable_metadata(root, item) for item in value]
    if key == 'path' or key.endswith('_path'):
        return _relative(root, value)
    return value


def _fields(root, run, metadata):
    result = []
    for item in metadata.get('field_outputs') or []:
        if isinstance(item, dict):
            relative = _relative(root, item.get('path'))
            if relative is not None:
                result.append({'path': relative, 'format': item.get('format'), 'fields': item.get('fields'),
                               'observation': 'caller_declared'})
    path = project.resolve_path(root, 'result/' + run['run_id'] + '/fields.json')
    if path.is_file():
        try:
            rows = json.loads(path.read_text(encoding='utf8'))
            groups = {}
            for row in rows:
                relative = _relative(root, row.get('artifact_path'))
                if relative is None or not project.resolve_path(root, relative).is_file():
                    continue
                names = groups.setdefault(relative, [])
                if row.get('name') is not None and row['name'] not in names:
                    names.append(row['name'])
            for relative, names in groups.items():
                if not any(entry['path'] == relative for entry in result):
                    result.append({'path': relative, 'format': Path(relative).suffix.lstrip('.') or None,
                                   'fields': names})
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    execution = run.get('execution_request') or {}
    if execution.get('toolchain', '').lower() == 'openfoam' and run.get('execution_case_path'):
        try:
            from .execution import _latest
            from .foam_readers import field_header
            latest = _latest(root, run)
            if latest['path'] is not None:
                directory = project.resolve_path(root, latest['path'])
                groups = {}
                entries = list(directory.iterdir())
                entries += [child for region in entries if region.is_dir() and not region.is_symlink()
                            for child in region.iterdir()]
                for field in entries:
                    if not field.is_file() or field.is_symlink() or not field.resolve().is_relative_to(directory):
                        continue
                    try:
                        with field.open('rb') as stream:
                            values, _ = field_header(stream.read(65536))
                    except (OSError, ValueError, UnicodeError):
                        continue
                    if (not values.get('class', '').endswith('Field') or values.get('object') != field.name
                            or values.get('format') not in ('ascii', 'binary')):
                        continue
                    parent = field.parent.relative_to(root).as_posix()
                    if _relative(root, parent) is not None:
                        groups.setdefault(parent, []).append(field.name)
                for relative, names in sorted(groups.items()):
                    if not any(entry['path'] == relative and entry.get('observation') == 'native_output_header' for entry in result):
                        result.append({'path': relative, 'format': 'openfoam', 'fields': sorted(names),
                                       'observation': 'native_output_header'})
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return result


def _mesh(run, metadata):
    """Keep caller descriptions separate from actual native checkMesh readback."""
    native = []
    def collect(steps, region=None):
        for step in steps:
            if isinstance(step, dict) and step.get('step') == 'checkMesh' and step.get('measurements') is not None:
                native.append({'region': region, 'exit_code': step.get('exit_code'),
                               'measurements': step['measurements']})
    collect((run.get('execution_state') or {}).get('steps') or [])
    for region, steps in (run.get('execution_steps') or {}).items():
        collect(steps, region)
    declared = metadata.get('mesh')
    return None if declared is None and not native else {'declared': declared, 'native_measured': native}


def _scalars(root, run):
    execution = run.get('execution_request') or {}
    monitors = execution.get('monitors') or []
    if execution.get('location', 'local') != 'local':
        return []  # remote monitors are rejected before launch until remote observations exist
    if not monitors:
        return []
    try:
        from .execution import _monitors
        return [{'name': row.get('name'), 'value': row.get('value'), 'unit': row.get('unit'),
                 'status': row.get('status')} for row in _monitors(root, monitors)]
    except (OSError, ValueError, TypeError, KeyError):
        return [{'name': row.get('name'), 'value': None, 'unit': row.get('unit'),
                 'status': 'unavailable'} for row in monitors if isinstance(row, dict)]


def _record(root, run, terminal):
    metadata = run.get('record_metadata') or {}
    execution = run.get('execution_request') or {}
    state = run.get('execution_state') or {}
    terminal = terminal or {}
    state_name = terminal.get('state') or state.get('state') or run.get('status')
    exit_code = terminal.get('exit_code') if terminal.get('exit_code') is not None else state.get('exit_code')
    termination = terminal.get('termination')
    if termination not in ('completed', 'user_cancelled', 'solver_failed', 'platform_failed'):
        if state_name == 'cancelled':
            termination = 'user_cancelled'
        elif state_name == 'completed' and exit_code in (None, 0):
            termination = 'completed'
        elif type(exit_code) is int and exit_code != 0:
            termination = 'solver_failed'
        else:
            termination = 'platform_failed'
    location = execution.get('location') or run.get('execution_location') or 'local'
    started = state.get('started_at') if location != 'local' else (state.get('started_at') or terminal.get('started_at'))
    finished = state.get('finished_at') if location != 'local' else (state.get('finished_at') or terminal.get('finished_at'))
    seconds = _seconds(started, finished)
    resource = run.get('resources') or execution.get('resources') or {}
    cores = resource.get('cores')
    if type(cores) is not int or cores < 1:
        cores = None
    hours = None if seconds is None or cores is None else seconds * cores / 3600
    products = run.get('tool_products', [])
    if run.get('producer'):
        path = project.resolve_path(root, '.navifield/tool-products/' + run['run_id'] + '.json')
        if path.is_file():
            products = json.loads(path.read_text(encoding='utf-8')).get('products', [])
    scalars = _scalars(root, run)
    scalars.extend(item for item in products if item.get('kind') == 'scalar')
    criteria_status = None
    declared = (execution.get('monitors') or [])
    monitored = {row.get('name') for row in declared if isinstance(row, dict) and row.get('criteria')}
    statuses = [row.get('status') for row in scalars if row.get('name') in monitored]
    if statuses:
        criteria_status = ('not_met' if 'not_met' in statuses else
                           'met' if len(statuses) == len(monitored) and all(s == 'met' for s in statuses)
                           else 'unavailable')
    lineage = dict(metadata.get('lineage') or {})
    if run.get('source_run_id') is not None:
        lineage['source_run_id'] = run['source_run_id']
    settings = metadata.get('settings_paths') or []
    return {'schema_version': 1, 'run_id': run['run_id'],
        'original_request': metadata.get('original_request'), 'conditions': _portable_metadata(root, metadata.get('conditions')),
        'geometry': _portable_metadata(root, metadata.get('geometry')), 'mesh': _portable_metadata(root, _mesh(run, metadata)),
        'solver': {'toolchain': execution.get('toolchain'), 'program': execution.get('program') or run.get('program'),
                   'argv': execution.get('argv', []),
                   'version': run.get('tool_version'),
                   'settings_paths': [path for value in settings if (path := _relative(root, value)) is not None]},
        'scalars': scalars, 'fields': _fields(root, run, metadata),
        'quality': {'completed': termination == 'completed', 'criteria_status': criteria_status,
                    'conservation': None, 'findings': list(run.get('findings') or [])},
        'lineage': _portable_metadata(root, lineage), 'resources': {'backend': 'local' if location == 'local' else 'ssh',
            'location': location,
            'cores': cores, 'started_at': started, 'finished_at': finished, 'wall_seconds': seconds,
            'core_hours': hours, 'measurement_basis': 'allocated_cores_times_observed_wall_seconds' if hours is not None else None,
            'termination': termination, 'exit_code': exit_code,
            **({'termination_reason': state.get('termination_reason'),
                'target_resource_control': state.get('resource_control'), 'cpu_selection': state.get('cpu_selection'),
                'start_observation': state.get('start_observation'),
                'native_launch_requested_at': state.get('native_launch_requested_at'),
                'exec_observation_delay_seconds': state.get('exec_observation_delay_seconds'),
                'platform_error': state.get('platform_error')}
               if location != 'local' else {})},
        'stopping': state.get('stopping'), 'termination_reason': state.get('termination_reason'),
        'input_snapshot': run.get('input_snapshot'),
        'monitor_tables': run.get('monitor_tables'),
        'decided_by': metadata.get('decided_by'), 'producer': run.get('producer'),
        'products': products}


def finalize_run_record(root, run_id, *, terminal=None):
    """Return warning findings on write failure without changing the original outcome."""
    try:
        root = Path(root).resolve()
        run = project.read_run(root, run_id)
        record = _record(root, run, terminal)
        warnings = []
        if record['quality']['completed'] and run.get('status') == 'completed':
            from .strip_results import index_run
            try:
                indexed = index_run(root, run)
                if indexed is not None:
                    record['numeric_result'] = indexed
            except (OSError, ValueError, KeyError, TypeError) as error:
                record['numeric_result'] = {'status': 'rejected', 'reason': str(error)}
                warnings.append({'code': 'NUMERIC_RESULT_REJECTED', 'severity': 'warn',
                                 'message': str(error), 'measured': None, 'limit': None, 'location': None})
        folder = project.resolve_path(root, '.navifield/records')
        folder.mkdir(parents=True, exist_ok=True)
        destination = project.resolve_path(root, '.navifield/records/' + run_id + '.json')
        temporary = folder / ('.' + run_id + '-' + uuid4().hex + '.tmp')
        with temporary.open('x', encoding='utf8') as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
        os.replace(temporary, destination)
        return warnings
    except Exception:
        return [_warning()]


def summarize_usage(root, *, reference_core_hour_price=None):
    """Aggregate known core hours only; report missing records/measurements separately."""
    if (reference_core_hour_price is not None and (type(reference_core_hour_price) not in (int, float)
            or not math.isfinite(reference_core_hour_price) or reference_core_hour_price < 0)):
        return {'status': 'failed', 'findings': [], 'artifacts': [], 'job_id': None, 'decision': None,
                'failure': {'code': 'INPUT_INVALID', 'stage': 'api', 'message': 'Reference price must be finite and nonnegative.',
                            'measured': None, 'limit': None, 'log_path': None, 'next_actions': []}, 'data': {}}
    root = Path(root).resolve()
    folder = project.resolve_path(root, '.navifield/records')
    rows, missing, recorded = [], 0, set()
    for path in sorted(folder.glob('r*.json')) if folder.is_dir() else ():
        recorded.add(path.stem)
        try:
            record = json.loads(path.read_text(encoding='utf8'))
            if record['run_id'] != path.stem:
                raise ValueError('Record identity does not match its filename.')
            resource = record['resources']
            hours = resource.get('core_hours')
            if type(hours) not in (int, float) or not math.isfinite(hours) or hours < 0:
                hours = None
            if hours is None:
                missing += 1
            rows.append({'run_id': record['run_id'], 'cores': resource.get('cores'),
                'wall_seconds': resource.get('wall_seconds'), 'core_hours': hours,
                'termination': resource.get('termination'),
                'reference_cost_cny': None if hours is None or reference_core_hour_price is None else hours * reference_core_hour_price})
        except (OSError, ValueError, KeyError, TypeError):
            missing += 1
    metadata_path = project.resolve_path(root, '.navifield/project.json')
    metadata = json.loads(metadata_path.read_text(encoding='utf8'))
    for manifest in metadata.get('runs', []):
        try:
            run = json.loads(project.resolve_path(root, manifest).read_text(encoding='utf8'))
            run_id = run['run_id']
            if (run.get('record_required') and run.get('status') in ('completed', 'failed', 'cancelled')
                    and run_id not in recorded):
                missing += 1
                rows.append({'run_id': run_id, 'cores': (run.get('resources') or {}).get('cores'),
                             'wall_seconds': None, 'core_hours': None, 'termination': None,
                             'reference_cost_cny': None, 'record_status': 'missing'})
        except (OSError, ValueError, KeyError, TypeError):
            continue
    known = [row['core_hours'] for row in rows if row['core_hours'] is not None]
    total = math.fsum(known) if known or missing == 0 else None
    data = {'runs': rows, 'total_core_hours': total, 'missing_measurement_count': missing}
    if reference_core_hour_price is None:
        for row in rows:
            row.pop('reference_cost_cny', None)
    else:
        data.update(total_reference_cost_cny=None if total is None else total * reference_core_hour_price,
                    reference_core_hour_price=reference_core_hour_price, currency='CNY',
                    price_basis='configurable reference price; not a bill')
    return {'status': 'ok', 'findings': [], 'artifacts': [], 'job_id': None, 'decision': None, 'failure': None,
            'data': data}
