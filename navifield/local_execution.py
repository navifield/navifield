"""Local-only CLI execution using the shared project, owner and native runner."""
from __future__ import annotations
import contextlib
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import time
from collections import deque

from . import project
from .backends import native_step, _Cancelled
from .foam_readers import parse_checkmesh
from .native_sequence import run_sequence
from .toolchains import resolve_tool, build_environment


def _ok(data=None, *, findings=(), artifacts=()):
    return {'status': 'ok', 'findings': list(findings), 'artifacts': list(artifacts),
            'job_id': None, 'decision': None, 'failure': None, 'data': data or {}}


def _fail(code, message):
    result = _ok()
    result.update(status='failed', failure={'code': code, 'stage': 'job', 'message': str(message),
                  'measured': None, 'limit': None, 'log_path': None, 'next_actions': []})
    return result


from .execution_common import _snapshot_inputs, _case_writer, _publish_residuals


def register_case(root, request):
    root = Path(root).resolve()
    try:
        case = project.resolve_path(root, request['path'])
        if not case.is_dir() or case == root: raise ValueError('A project-local case directory is required.')
        folder = project.resolve_path(root, '.navifield/cases'); folder.mkdir(parents=True, exist_ok=True)
        ids = [int(p.stem[1:]) for p in folder.glob('c*.json') if re.fullmatch(r'c[0-9]+', p.stem)]
        record = {'case_id': f'c{max(ids, default=0)+1:03d}', 'path': request['path'],
                  'name': request.get('name'), 'toolchain': request.get('toolchain')}
        path = folder / (record['case_id'] + '.json')
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temporary, path)
        return _ok(record)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return _fail('INPUT_INVALID', exc)


def prepare_run(root, request):
    root = Path(root).resolve()
    try:
        case_id = request['case_id']
        if not re.fullmatch(r'c[0-9]{3,}', case_id): raise ValueError('Invalid case identifier.')
        case = json.loads(project.resolve_path(root, f'.navifield/cases/{case_id}.json').read_text(encoding='utf-8'))
        path = case['path']
        if not project.resolve_path(root, path).is_dir(): raise ValueError('Registered case directory is unavailable.')
        spec = {'case_id': case_id, 'toolchain': request['toolchain'], 'program': request['program'],
                'argv': request.get('argv', []), 'resources': request.get('resources') or {'cores': 1},
                'purpose': 'tool', 'cli_inputs': request.get('cli_inputs', []), 'location': 'local',
                'monitors': [], 'parallel': None, 'stop_when': None}
        if not all(isinstance(v, str) for v in spec['argv']): raise ValueError('Arguments must be strings.')
        run = project.allocate_run(root, 'generic', path)
        run.update(status='queued', stage='execute', case_id=case_id, program=spec['program'],
                   argv=spec['argv'], execution_location='local', source_run_id=None,
                   source_case_path=path, execution_case_path=path, execution_request=spec,
                   resources=spec['resources'], record_metadata={'original_request': request.get('original_request'),
                   'conditions': None, 'geometry': None, 'mesh': None, 'field_outputs': None,
                   'lineage': {}, 'decided_by': None}, record_required=True,
                   execution_state={'state': 'queued', 'step': None, 'exit_code': None, 'job_id': None,
                                    'started_at': None, 'finished_at': None, 'log_path': None})
        project.save_run(root, run)
        return _ok({'run_id': run['run_id'], 'state': 'queued'})
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return _fail('INPUT_INVALID', exc)



def _latest(root, run):
    if (run.get('execution_request') or {}).get('toolchain', '').lower() != 'openfoam':
        return {'value': None, 'path': None, 'reason': 'No generic checkpoint reader for this toolchain.'}
    case = project.resolve_path(root, run['execution_case_path']); choices = []
    for path in case.iterdir():
        try: value = float(path.name)
        except ValueError: continue
        if path.is_dir() and math.isfinite(value): choices.append((value, path))
    if not choices: return {'value': None, 'path': None, 'reason': 'No written numeric time directory.'}
    value, path = max(choices, key=lambda pair: pair[0])
    return {'value': value, 'path': path.relative_to(root).as_posix(), 'reason': None}


from .monitor_evaluation import _monitors


def run_status(root, run_id, request=None):
    try:
        root = Path(root).resolve(); run = project.read_run(root, run_id)
        state = dict(run.get('execution_state') or {}); state['run_id'] = run_id
        state.setdefault('stopping', {'requested': False, 'reason': None, 'requested_at': None,
                         'trigger_path': None, 'delivery': 'not_requested', 'confirmed': False,
                         'observed_at': None, 'monitor_names': [], 'observations': []})
        state['rid'] = project.open_project(root)['project_id'] + '~' + run_id
        state['engineering_acceptance'] = 'NOT_EVALUATED'
        if (run.get('execution_request') or {}).get('stop_when') is not None:
            state['latest_written'] = state.get('live_checkpoint') or {'value': None, 'path': None,
                                       'reason': 'No new serial field checkpoint observed.'}
        else:
            state['latest_written'] = _latest(root, run)
        if run.get('resume_requested'):
            state.update(selected_checkpoint=run['selected_checkpoint'], resume_requested=True,
                         resume_confirmed=run.get('resume_confirmed', False), actual_start=run.get('actual_start'),
                         resume_status='confirmed' if run.get('resume_confirmed') else 'requested_not_confirmed')
        state['findings'] = run.get('findings', []); state['artifacts'] = run.get('artifacts', [])
        state['monitors'] = _monitors(root, (run.get('execution_request') or {}).get('monitors', [])); state['residuals_path'] = run.get('residuals_path')
        state['log_tail'] = []
        if state.get('log_path'):
            path = project.resolve_path(root, state['log_path'])
            if path.exists():
                with path.open(encoding='utf-8', errors='replace') as stream:
                    state['log_tail'] = [line.rstrip('\r\n') for line in deque(stream, maxlen=200)]
        return _ok(state, findings=run.get('findings', []), artifacts=run.get('artifacts', []))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return _fail('NOT_FOUND', exc)


def execute_run(root, request):
    root = Path(root).resolve(); run = None
    try:
        run = project.read_run(root, request['run_id']); spec = run['execution_request']
        if spec.get('location', 'local') != 'local': raise ValueError('Only local runs are supported.')
        executable, config = resolve_tool(request.get('_toolchains', {}), spec['toolchain'], spec['program'])
        env = build_environment(config, threads={'OMP_NUM_THREADS': str(spec['resources'].get('cores', 1)),
                                                  'MKL_NUM_THREADS': str(spec['resources'].get('cores', 1))})
        case = project.resolve_path(root, run['execution_case_path']); state = run['execution_state']
        state.update(state='running', step='execute', started_at=None, job_id=request.get('job_id'),
                     log_path='result/' + run['run_id'] + '/native.log')
        run['status'] = 'running'; project.save_run(root, run)
        log = project.resolve_path(root, state['log_path'])
        run['artifacts'].append({'path': state['log_path'], 'kind': 'log', 'media_type': 'text/plain',
                                 'run_id': run['run_id'], 'label': 'Native log'})
        def clean(text, base):
            text = text.replace(str(base), '.')
            text = re.sub(r'(?i)(authorization\s*[:=]\s*(?:bearer\s+)?|api[_-]?key\s*[:=]\s*)[^\s,;]+', r'\1[redacted]', text)
            return re.sub(r'(?i)(?:[A-Z]:[\\/]|/)\S*[\\/](?:backend|scripts)[\\/]\S+', '[internal source]', text)
        def launch(exe, args, **options):
            return native_step(exe, args, clean=clean, **options)
        def on_started():
            if state['started_at'] is None:
                state['started_at'] = project._now(); project.save_run(root, run)
        with _case_writer(root, case):
            _snapshot_inputs(root, run)
            project.save_run(root, run)
            with log.open('w', encoding='utf-8') as output:
                steps, findings, code = run_sequence(case, env, [('execute', executable, spec['argv'], env)], output,
                    native_step=launch, parse_checkmesh=parse_checkmesh, cancelled_error=_Cancelled, root=root, on_started=on_started)
        state.update(steps=steps, exit_code=code, state='completed' if code == 0 else 'failed', finished_at=project._now())
        run['findings'].extend(findings); run['status'] = state['state']
        _publish_residuals(root, run, log)
        project.save_run(root, run)
        return _ok(run_status(root, run['run_id'])['data']) if code == 0 else _fail('SOLVER_FAILED', 'Native program returned a nonzero exit code.')
    except (OSError, ValueError, KeyError, TypeError) as exc:
        if run is not None:
            run['status'] = 'failed'; run['execution_state'].update(state='failed', finished_at=project._now())
            project.save_run(root, run)
        return _fail('ENGINE_UNAVAILABLE', exc)
