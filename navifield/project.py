"""Portable project and run records for the agent engine."""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .project_template import init_project


def _now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def resolve_path(root, path):
    """Resolve a short project-relative path without allowing escape."""
    p = PurePosixPath(path)
    if not path or len(path) > 120 or p.is_absolute() or '\\' in path or ':' in path or any(x in ('.', '..') for x in p.parts):
        raise ValueError('invalid project-relative path')
    base = Path(root).resolve()
    target = (base / Path(*p.parts)).resolve()
    if not target.is_relative_to(base):
        raise ValueError('path escapes project')
    return target


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def open_project(root):
    return init_project(root)


def allocate_run(root, analysis_type, case_path, resume_from_run_id=None):
    root = Path(root)
    project = open_project(root)
    resolve_path(root, case_path)
    number = project['next_run_number']
    run_id = f'r{number:03d}'
    manifest_path = f'.navifield/runs/{run_id}/manifest.json'
    now = _now()
    manifest = {'schema_version': 1, 'run_id': run_id, 'analysis_type': analysis_type,
                'created_at': now, 'updated_at': now, 'status': 'running', 'stage': 'geometry',
                'case_path': case_path, 'accepted_geometry_revision': None,
                'resume_from_run_id': resume_from_run_id, 'artifacts': [], 'findings': [],
                'decisions': [], 'jobs': [], 'continuation': None}
    for directory in ('temporal', 'model', 'result', 'report', 'simulation_setup'):
        (root / directory / run_id).mkdir(parents=True, exist_ok=True)
    _write(resolve_path(root, manifest_path), manifest)
    project['runs'].append(manifest_path)
    project['next_run_number'] = number + 1
    project['updated_at'] = now
    _write(root / '.navifield/project.json', project)
    return manifest


def read_run(root, run_id):
    if not run_id.startswith('r') or not run_id[1:].isdigit() or len(run_id) < 4:
        raise ValueError('invalid run id')
    path = resolve_path(root, f'.navifield/runs/{run_id}/manifest.json')
    return json.loads(path.read_text(encoding='utf-8'))


def save_run(root, manifest):
    run_id = manifest['run_id']
    read_run(root, run_id)
    manifest['updated_at'] = _now()
    _write(resolve_path(root, f'.navifield/runs/{run_id}/manifest.json'), manifest)


def publish_file(root, run_id, source, category, name, kind="data", media_type="application/octet-stream", label=""):
    read_run(root, run_id)
    if category not in ('model', 'result', 'report'):
        raise ValueError('invalid publication category')
    relative = f'{category}/{run_id}/{name}'
    destination = resolve_path(root, relative)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return {"path": relative, "kind": kind, "media_type": media_type, "run_id": run_id, "label": label}

