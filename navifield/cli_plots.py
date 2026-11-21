"""Optional charts from the selected CLI run's retained log and numeric tables."""
from pathlib import Path

from . import project
from .live_monitor import _follower, run_tables
from .postprocess import EngineeringReport, ReportFigure, ReportParagraph, ReportSection, _digest, _html


def generate(root, rid):
    try:
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure
    except ImportError as error:
        raise RuntimeError('Plotting dependencies are unavailable; install navifield[plots]. ' + str(error)) from error
    root = Path(root).resolve()
    follower, run, _ = _follower(root, rid)
    follower.poll()
    tables, source = run_tables(root, run)
    charts = []
    if follower.order:
        charts.append(('Initial residuals (OpenFOAM-style log)', [(key, follower.series[key]) for key in follower.order[:8]], True))
    for table in tables:
        for column in table['columns']:
            charts.append((table['name'] + ' / ' + column['label'], [(column['label'], column['points'])], False))
    if not charts:
        raise ValueError('No residual or numeric postProcessing table is available for this run.')
    output_dir = project.resolve_path(root, f'report/{rid}'); output_dir.mkdir(parents=True, exist_ok=True)
    blocks = []
    for index, (title, lines, logarithmic) in enumerate(charts[:24]):
        figure = Figure(figsize=(8, 4.2), dpi=110); FigureCanvasAgg(figure); axes = figure.subplots()
        for label, points in lines:
            axes.plot([p[0] for p in points], [p[1] for p in points], label=label)
        if logarithmic and all(value > 0 for _, points in lines for _, value in points):
            axes.set_yscale('log')
        axes.set_xlabel('Step / time (as recorded)'); axes.set_ylabel('Initial residual' if logarithmic else 'Value (unit not inferred)')
        axes.set_title(title); axes.grid(True, alpha=.3); axes.legend(); figure.tight_layout()
        relative = f'report/{rid}/plot-{index+1:02d}.png'; path = project.resolve_path(root, relative)
        figure.savefig(path)
        blocks.append(ReportFigure(relative_path=relative, sha256=_digest(path), caption=title, alt_text=title))
    descriptions = {
        'run': 'Numeric tables are the copies kept with this run.',
        'run_partial': 'The run table copy is incomplete; only available retained tables are shown, without filling gaps from the current case.',
        'run_unavailable': 'The run table copy is unavailable; current case tables are not substituted.',
        'case': 'This legacy run has no table copy; numeric tables are the current contents of its case directory and may have changed during later runs.',
        None: 'No numeric table source is available for this run.',
    }
    note = (f'Residuals are read from the selected run main log. {descriptions[source]} '
            f'Shown {min(len(charts), 24)} of {len(charts)} available charts. '
            'Units and physical interpretation are not inferred. '
            'Residual thresholds do not establish engineering acceptance.')
    report = EngineeringReport(summary=f'Run {rid}: selected run log and numeric tables ({source or "unavailable"})', sections=(
        ReportSection(key='charts', title='Recorded histories', blocks=tuple(blocks)),
        ReportSection(key='scope', title='Scope', blocks=(ReportParagraph(text=note),)),))
    output = output_dir / 'plots.html'
    output.write_text(_html(report, root, output, self_contained=True, title='Run histories'), encoding='utf-8')
    return output.relative_to(root).as_posix()
