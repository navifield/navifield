"""Explicit OpenFOAM velocity section reports, with source provenance."""
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import tempfile

from . import project
from .foam_readers import complete_internal_field, field_header, open_native_foam
from .postprocess import EngineeringReport, ReportFigure, ReportParagraph, ReportSection, _digest, _html

_TIME = re.compile(r'^(?:0|[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$')
_MESH = ('points', 'faces', 'owner', 'neighbour', 'boundary')


def _time_mesh_at_or_before(name, requested):
    parts = name.split('/')
    return (len(parts) >= 2 and _TIME.fullmatch(parts[0]) is not None
            and float(parts[0]) <= requested and parts[1] == 'polyMesh')


def resolve_source(root, rid, time_name, field_case=None):
    """A recorded input snapshot or an explicitly selected project case, never cwd fallback."""
    run = project.read_run(root, rid)
    if not isinstance(time_name, str) or not _TIME.fullmatch(time_name) or not math.isfinite(float(time_name)):
        raise ValueError('Select an explicit finite nonnegative OpenFOAM time directory.')
    needed = ['system/controlDict', *(f'constant/polyMesh/{name}' for name in _MESH), f'{time_name}/U']
    if field_case is not None:
        base = project.resolve_path(root, field_case)
        kind = 'user_selected_case'
        note = ('User-selected case: association with this run is unverified; '
                'the field may have been changed or produced by another run.')
    else:
        snapshot = run.get('input_snapshot') or {}
        if not isinstance(snapshot.get('path'), str):
            raise ValueError('No retained input snapshot for this run; provide --field-case explicitly.')
        copied = {x for x in snapshot.get('copied', []) if isinstance(x, str)}
        if not all(name in copied for name in needed):
            raise ValueError('Selected time U or mesh is absent from this run input snapshot; '
                             'provide an explicit --field-case or select the run that retained it.')
        # A time-varying mesh could have been copied or skipped: no silent static-mesh substitution.
        all_records = list(snapshot.get('copied', [])) + list(snapshot.get('skipped', []))
        names = [x if isinstance(x, str) else x.get('path', '') for x in all_records if isinstance(x, (str, dict))]
        if any(_time_mesh_at_or_before(name, float(time_name)) for name in names):
            raise ValueError('Time-varying mesh is not supported for this field report.')
        base = project.resolve_path(root, snapshot['path'])
        kind = 'run_input_snapshot'
        note = ('Selected run input snapshot captured at run start; it is not a retained solver output. '
                'The field association with any producing run and snapshot integrity are unverified.')
    for directory in base.iterdir():
        if (directory.is_dir() and _TIME.fullmatch(directory.name)
                and float(directory.name) <= float(time_name) and (directory / 'polyMesh').exists()):
            raise ValueError('Time-varying mesh is not supported for this field report.')
    for name in needed:
        target = project.resolve_path(root, (base / name).relative_to(root).as_posix())
        if not target.is_file():
            raise ValueError(f'Required OpenFOAM file is unavailable: {name}')
    ufile = base / time_name / 'U'
    with ufile.open('rb') as stream:
        header, dims = field_header(stream.read(65536))
    if (header.get('class') != 'volVectorField' or header.get('object') != 'U'
            or header.get('format') != 'ascii' or dims != (0., 1., -1., 0., 0., 0., 0.)
            or not complete_internal_field(ufile, 'volVectorField')):
        raise ValueError('U must be a complete ASCII volVectorField with velocity dimensions [0 1 -1 0 0 0 0].')
    return base, needed, kind, note


def generate(root, rid, time_name, *, field_case=None, axis=None, position=None):
    root = Path(root).resolve()
    base, needed, kind, note = resolve_source(root, rid, time_name, field_case)
    try:
        import numpy as np
        from .core_section import axis_section
        from .core_render import _render_one
    except ImportError as error:
        raise RuntimeError('Field plotting dependencies unavailable; install navifield[plots].') from error
    # Isolate the selected time and files: no later field or unrelated case data is read.
    with tempfile.TemporaryDirectory(prefix='navifield-native-field-') as tmp:
        stage = Path(tmp)
        for name in needed:
            dest = stage / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(base / name, dest)
        mesh = open_native_foam(stage, float(time_name), cell_arrays=['U'])
        if 'U' not in mesh.cell_data:
            raise ValueError('Velocity U cell data is not available at the selected time.')
        array = np.asarray(mesh.cell_data['U'], dtype=float)
        if array.shape != (mesh.n_cells, 3) or not np.isfinite(array).all():
            raise ValueError('Velocity U must contain finite three-component cell vectors.')
        bounds = mesh.bounds
        if not all(math.isfinite(v) for v in bounds):
            raise ValueError('Mesh bounds are not finite.')
        spans = [bounds[2*i+1] - bounds[2*i] for i in range(3)]
        chosen = axis or 'xyz'[int(np.argmin(spans))]
        if chosen not in ('x', 'y', 'z'):
            raise ValueError('Select axis x, y, or z.')
        index = 'xyz'.index(chosen)
        pos = (bounds[2*index] + bounds[2*index+1]) / 2 if position is None else position
        if not math.isfinite(pos) or not bounds[2*index] < pos < bounds[2*index+1]:
            raise ValueError('Section position must be inside the mesh bounds.')
        section = axis_section(mesh, chosen, pos)
        if not section.n_cells or 'U' not in section.cell_data:
            raise ValueError('Velocity section is empty or lacks native cell data.')
        values = np.asarray(section.cell_data['U'], dtype=float)
        if values.shape != (section.n_cells, 3) or not np.isfinite(values).all():
            raise ValueError('Velocity section does not preserve finite cell vectors.')
        speed = np.linalg.norm(values, axis=1)
        section.cell_data['speed'] = speed
        low, high = float(speed.min()), float(speed.max())
        png = stage / 'velocity.png'
        _render_one(section, 'speed', chosen.upper(), str(png), bar_title='Speed magnitude (m/s)',
                    time_tag=time_name, association='cell', isometric=True,
                    scalar_range=(low, high) if low < high else None, plane_normal_view=True,
                    annotation=f'{rid} | {kind} | native cell\n{chosen} = {pos:.6g}; range {low:.6g} to {high:.6g} m/s')
        figure_hash = _digest(png)
        identity = json.dumps({'time': time_name, 'kind': kind, 'source': base.relative_to(root).as_posix(),
                               'axis': chosen, 'position': pos, 'figure': figure_hash}, sort_keys=True)
        tag = hashlib.sha256(identity.encode('utf-8')).hexdigest()[:16]
        outdir = project.resolve_path(root, f'report/{rid}'); outdir.mkdir(parents=True, exist_ok=True)
        output_png = outdir / f'velocity-{tag}.png'
        shutil.copyfile(png, output_png)
    info = {'run_id': rid, 'time_directory': time_name, 'field': 'U', 'unit': 'm/s',
            'source_kind': kind, 'source_path': base.relative_to(root).as_posix(),
            'source_note': note, 'produced_by_run_verified': False,
            'association': 'native cell', 'section_axis': chosen, 'section_position_mesh_units': pos,
            'speed_min_m_s': low, 'speed_max_m_s': high,
            'figure': output_png.relative_to(root).as_posix()}
    detail = outdir / f'velocity-{tag}.json'
    detail.write_text(json.dumps(info, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    caption = f'{rid} | {kind} | U magnitude (m/s) | time {time_name} | {chosen}={pos:.6g} (mesh coordinates); native cell range {low:.6g} to {high:.6g} m/s. {note}'
    report = EngineeringReport(summary=f'Velocity field for selected run {rid}; provenance limitations apply.', sections=(
        ReportSection(key='field', title='Native cell velocity section', blocks=(
            ReportFigure(relative_path=info['figure'], sha256=figure_hash, caption=caption, alt_text=caption),)),
        ReportSection(key='source', title='Source and limitations', blocks=(
            ReportParagraph(text=f'Section {chosen}={pos:.6g} in mesh coordinates; native cell speed range {low:.6g} to {high:.6g} m/s.'),
            ReportParagraph(text=note),)),))
    html = outdir / f'velocity-{tag}.html'
    html.write_text(_html(report, root, html, self_contained=True, title='Velocity field'), encoding='utf-8')
    return {'report': html.relative_to(root).as_posix(), 'details': detail.relative_to(root).as_posix(),
            'figure': info['figure'], 'source_kind': kind, 'produced_by_run_verified': False}
