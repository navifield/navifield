"""Run local solver commands with durable records, without a service."""
from __future__ import annotations
import argparse
import base64
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import tempfile

from . import local_execution as execution, project, records
from .job_manager import JobOwner


def _write(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def _detail(root, rid, name, value):
    """Save a complete machine-readable snapshot in the project's report directory."""
    path = project.resolve_path(root, f'report/{rid}/{name}.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path.relative_to(root).as_posix()

def _short_wait(root, rid, summary):
    # Keep the established small wait response unchanged.
    if len(json.dumps(summary)) <= 2048:
        return summary
    detail = _detail(root, rid, 'wait-summary', summary)
    result = {key: summary.get(key) for key in ('rid', 'state', 'reason', 'waited_seconds',
              'exit_code', 'last_step', 'fatal', 'stop_requested', 'log')}
    residuals, monitors = summary.get('residuals') or {}, summary.get('monitors') or {}
    sample = {}
    for name, columns in list(monitors.items())[:2]:
        if isinstance(columns, dict):
            sample[name[:120]] = {label[:120]: value for label, value in list(columns.items())[:1]}
    result.update(residuals={name[:120]: value for name, value in list(residuals.items())[:8]},
                  residual_count=len(residuals), monitors=sample, monitor_table_count=len(monitors),
                  monitor_column_count=sum(len(c) for c in monitors.values() if isinstance(c, dict)),
                  truncated=True, details=detail)
    stages = summary.get('stages')
    if stages:
        steps = stages['steps']
        result['stages'] = {'path': stages['path'], 'count': len(steps),
                            'current': [step for step in steps if step.get('state') == 'running'],
                            'recent': steps[-3:]}
    return result

def _location(root, rid):
    project.read_run(root, rid)
    return project.resolve_path(root, f'.navifield/runs/{rid}')


def _controller_run(root, rid, executable):
    run = project.read_run(root, rid)
    folder = _location(root, rid)
    env_file = folder / 'controller-env.json'
    if env_file.is_file():
        os.environ.update(json.loads(env_file.read_text(encoding='utf-8')))
    resources = run['resources']
    owner = JobOwner(folder / 'owner', logging.getLogger('navifield.cli'),
                     worker_module=__package__ + '.local_worker')
    job_id = 'local'
    payload = {'root': str(root), 'operation': 'run:execute',
        'body': {'run_id': rid, '_cli_direct': True}, 'principal': 'local',
        'resources': resources, 'location': 'local',
        'toolchains': {'local': {'roots': [str(executable.parent)], 'environment': {}}}}
    import psutil
    current = psutil.Process()
    try:
        parent = current.parent()
        parent_identity = {'parent_pid': parent.pid, 'parent_created': parent.create_time()} if parent else {}
    except psutil.Error:
        parent_identity = {}
    identity = {'pid': os.getpid(), 'created': current.create_time(), **parent_identity}
    _write(folder / 'controller.json', {**identity, 'state': 'running'})
    owner.submit(job_id, payload, on_started=lambda *_: None, wall_seconds=resources.get('wall_seconds'))
    state = owner.states[job_id]
    try:
        while not state.done.wait(.1):
            if (folder / 'stop.request').exists():
                owner.cancel(job_id)
    except KeyboardInterrupt:
        owner.cancel(job_id)
    outcome = owner.wait(job_id)
    run = project.read_run(root, rid)
    if 'monitor_tables' not in run:
        run['monitor_tables'] = {'path': f'result/{rid}/monitor-tables', 'copied': [],
                                 'skipped': [{'path': '.', 'reason': 'worker_ended_before_snapshot'}]}
        project.save_run(root, run)
    if outcome.status != 'complete':
        run['status'] = 'cancelled' if outcome.status == 'cancelled' else 'failed'
        run['execution_state'].update(state=run['status'], finished_at=project._now(),
                                      exit_code=outcome.exit_code if outcome.status == 'cancelled' else None,
                                      worker_exit_code=outcome.exit_code, platform_error=outcome.error)
        project.save_run(root, run)
    warnings = records.finalize_run_record(root, rid)
    if warnings:
        run = project.read_run(root, rid)
        run['findings'].extend(warnings)
        project.save_run(root, run)
    state = project.read_run(root, rid)['execution_state']
    _write(folder / 'controller.json', {**identity, 'state': state['state']})
    code = state.get('exit_code')
    if state['state'] == 'failed' and code == 0:
        return 1
    return code if type(code) is int and 0 <= code <= 0xffffffff else (0 if state['state'] == 'completed' else 1)


def _controller(root, rid, executable):
    try:
        return _controller_run(root, rid, executable)
    except BaseException as error:
        folder = _location(root, rid)
        _write(folder / 'controller-failure.json', {'type': type(error).__name__, 'message': str(error),
                                                     'state': 'unknown'})
        raise


def _controller_matches(state, launcher_pid, launcher_created, psutil, *, allow_exited=False):
    """Match the CIM process or its recorded Python child, including creation times."""
    child_pid, child_created = state.get('pid'), state.get('created')
    if not isinstance(child_pid, int) or not isinstance(child_created, (int, float)):
        return False
    if child_pid == launcher_pid:
        if abs(child_created - launcher_created) >= .001:
            return False
        try:
            return abs(psutil.Process(child_pid).create_time() - child_created) < .001
        except psutil.NoSuchProcess:
            return allow_exited
    if (state.get('parent_pid') != launcher_pid or
            not isinstance(state.get('parent_created'), (int, float)) or
            abs(state['parent_created'] - launcher_created) >= .001 or child_created < launcher_created):
        return False
    try:
        child = psutil.Process(child_pid)
        return child.ppid() == launcher_pid and abs(child.create_time() - child_created) < .001
    except psutil.NoSuchProcess:
        return allow_exited


def _windows_background(root, rid, executable, folder):
    keys = {'PATH', 'PATHEXT', 'PYTHONPATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'TEMP', 'TMP'}
    env = {key: value for key, value in os.environ.items()
           if key.upper() in keys or key.upper().startswith(('WM_', 'FOAM_', 'MSMPI_'))}
    _write(folder / 'controller-env.json', env)
    bootstrap = ("import sys;sys.path.insert(0,sys.argv[1]);"
                 "sys.stdout=open(sys.argv[2],'a',encoding='utf-8',buffering=1);sys.stderr=sys.stdout;"
                 "from " + __package__ + ".local_cli import main;sys.exit(main(sys.argv[3:]))")
    command = subprocess.list2cmdline([sys.executable, '-c', bootstrap, str(Path(__file__).resolve().parents[len(__package__.split('.'))]),
        str(folder / 'controller.log'), '_controller', '--project', str(root), '--rid', rid, '--exe', str(executable)])
    quote = lambda value: "'" + value.replace("'", "''") + "'"
    script = ("$s=New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ShowWindow=[uint16]0};"
              "$r=Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments "
              "@{CommandLine=" + quote(command) + ";CurrentDirectory=" + quote(str(root)) +
              ";ProcessStartupInformation=$s};if($r.ReturnValue -ne 0){exit 1};Write-Output $r.ProcessId")
    encoded = base64.b64encode(script.encode('utf-16le')).decode('ascii')
    launched_at = time.time()
    result = subprocess.run(['powershell.exe', '-NoProfile', '-EncodedCommand', encoded],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode:
        raise RuntimeError('Could not start the local background controller: ' + result.stderr.strip())
    try:
        import psutil
        pid = int(result.stdout.strip().splitlines()[-1])
        try:
            proc = psutil.Process(pid)
            created = proc.create_time()
        except psutil.NoSuchProcess:
            created = None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                state = json.loads((folder / 'controller.json').read_text(encoding='utf-8'))
                if created is None:
                    claimed = state.get('parent_created') if state.get('parent_pid') == pid else state.get('created')
                    if not isinstance(claimed, (int, float)) or not launched_at - 1 <= claimed <= time.time():
                        raise ValueError('controller creation time is outside this launch')
                    created = claimed
                terminal = state.get('state') in ('completed', 'failed', 'cancelled')
                recorded = False
                if terminal:
                    owner = json.loads((folder / 'owner' / 'local' / 'owner.json').read_text(encoding='utf-8'))
                    recorded = (owner.get('phase') == 'empty' and
                                (root / '.navifield' / 'records' / (rid + '.json')).is_file())
                if ((not terminal or recorded) and
                        _controller_matches(state, pid, created, psutil, allow_exited=recorded)):
                    return
            except (OSError, ValueError, TypeError):
                pass
            time.sleep(.05)
    except (IndexError, ValueError, psutil.Error) as error:
        raise RuntimeError('Background controller identity unavailable.') from error
    raise RuntimeError('Background controller did not confirm startup; inspect controller.log.')


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(prog='navifield')
    sub = parser.add_subparsers(dest='action', required=True)
    fin = sub.add_parser('fin', help='Model a steady cylindrical fin in SI units and read its CalculiX output')
    fin_actions = fin.add_subparsers(dest='fin_action', required=True)
    fin_build = fin_actions.add_parser('build', help='Generate a DC3D8 input deck from a JSON model')
    fin_build.add_argument('model', type=Path, help='UTF-8 model JSON')
    fin_build.add_argument('--deck', type=Path, required=True, help='New CalculiX .inp file')
    fin_read = fin_actions.add_parser('read', help='Read a generated deck and the matching native .dat result')
    fin_read.add_argument('--deck', type=Path, required=True)
    fin_read.add_argument('--dat', type=Path, required=True)
    fin_read.add_argument('--positions-m', nargs='+', type=float, default=[],
                          help='Axial observation positions measured from the base, in metres')
    route = sub.add_parser('route', help='Match structured physics requirements to evidenced routes')
    route.add_argument('requirement', nargs='?', help='UTF-8 JSON requirement file')
    route.add_argument('--catalog', action='store_true', help='List supported tokens')
    route.add_argument('--brief', action='store_true', help='Concise SQLite dependency report')
    route.add_argument('--emit-gaps', help='Write controlled research gap JSON')
    route.add_argument('--with-evidence', help='Bind external research findings to current gaps')
    route.add_argument('--research-cache', help='Local cache of time-bounded externally verified findings')
    strip = sub.add_parser('prepare-strip', help='Compile a confirmed 1D axial electrothermal strip without running a solver')
    strip.add_argument('requirement', help='UTF-8 JSON requirement and normalized SI strip object')
    strip.add_argument('--confirm', help='Confirmation SHA-256 from route --brief')
    strip.add_argument('--output', required=True, help='New output directory')
    capabilities = sub.add_parser('capabilities', help='Query the offline indexed capability library')
    caps = capabilities.add_subparsers(dest='capability_action', required=True)
    cap_list = caps.add_parser('list', help='Browse indexed software and capability records')
    cap_list.add_argument('--solver', help='List records for one software identifier')
    for name, help_text in [('modules', 'Show manual coverage and open gaps'),
                            ('couplings', 'Show directed field transfers and custom work')]:
        view = caps.add_parser(name, help=help_text)
        view.add_argument('--solver', help='Filter by software identifier')
        if name == 'couplings':
            view.add_argument('--field', help='Match part of a source field, target field, or exchanged quantity')
            view.add_argument('--implementation', choices=('native', 'configured', 'adapter', 'custom'))
    gap_view = caps.add_parser('gaps', help='Look up assessed gaps with reasons and version evidence')
    gap_view.add_argument('--solver')
    from .capability_kb import FEATURE_STATUSES, GAP_STATUSES
    gap_view.add_argument('--status', choices=GAP_STATUSES)
    summary_view = caps.add_parser('feature-summary', help='Count indexed upstream features by coverage status')
    summary_view.add_argument('--solver')
    summary_view.add_argument('--by-index', action='store_true', help='Include source scope and counts for each reference index')
    features_view = caps.add_parser('features', help='Search the versioned reference inventory')
    features_view.add_argument('--solver')
    features_view.add_argument('--index', help='Limit results to a named reference index')
    features_view.add_argument('--term', default='')
    features_view.add_argument('--status', choices=FEATURE_STATUSES)
    features_view.add_argument('--capability', help='Reverse lookup of an engineering capability ID')
    features_view.add_argument('--limit', type=int, default=20)
    features_view.add_argument('--offset', type=int, default=0)
    paper = caps.add_parser('paper-cases', help='Find engineering cases in primary research papers')
    paper.add_argument('--software', help='Software reported in the paper, e.g. Fluent or COMSOL')
    paper.add_argument('--target', help='Physical quantity, e.g. pressure_drop or wall_temperature')
    paper.add_argument('--industry', help='Exact industry or parent of subindustries separated by \' / \'')
    paper.add_argument('--id', help='Return one complete case with evidence and limitations')
    paper.add_argument('--doi', help='Return the complete cases reported in this publication')
    paper.add_argument('--term', help='Match a title, author, case ID or indexed keyword')
    caps.add_parser('target-schema', help='Public goals, applicability conditions, inputs and field ports')
    target_view = caps.add_parser('target-inventory', help='Inspect a versioned output, model and its alternatives')
    target_view.add_argument('--solver')
    target_view.add_argument('--version')
    target_view.add_argument('--target')
    chain_inventory = caps.add_parser('chain-inventory', help='Inspect versioned equipment equation chains')
    chain_inventory.add_argument('--solver')
    chain_inventory.add_argument('--version')
    chain_inventory.add_argument('--chain', help='Return ordered equations, closures, inputs and their evidence')
    chain_query = caps.add_parser('select-chains', help='Select an equipment chain by desired output and operating regime')
    chain_query.add_argument('requirement', help='Structured UTF-8 JSON, see examples/cli-v01/device-chains')
    chain_query.add_argument('--solver')
    chain_query.add_argument('--version')
    chain_query.add_argument('--brief', action='store_true', help='Return the best supported candidates and gaps')
    phase = caps.add_parser('phase-support', help='Review versioned melting, boiling and condensation support')
    phase.add_argument('--solver')
    phase.add_argument('--version')
    from .model_selection import PHASE_PHENOMENA, SUPPORT_STATUSES
    phase.add_argument('--phenomenon', choices=PHASE_PHENOMENA)
    phase.add_argument('--status', choices=SUPPORT_STATUSES)
    model_view = caps.add_parser('model-profiles', help='Inspect model assumptions, inputs and sources')
    model_view.add_argument('--domain')
    model_view.add_argument('--solver')
    model_view.add_argument('--version')
    model_view.add_argument('--model')
    model_query = caps.add_parser('select-models', help='Select models by physical mechanism, goal and applicability')
    model_query.add_argument('requirement', help='Structured UTF-8 JSON, see examples/cli-v01/model-selection')
    model_query.add_argument('--solver')
    model_query.add_argument('--version')
    model_query.add_argument('--brief', action='store_true', help='Best matching records with shared source references')
    for selector in (model_query, chain_query):
        selector.add_argument('--emit-gaps', help='Write bound gaps for external source research')
        selector.add_argument('--with-evidence', help='Read findings for exactly this selector, version and scope')
        selector.add_argument('--research-cache', help='Reuse current, scoped research findings')
    profile = caps.add_parser('turbulence-profiles', help='List versioned profiles or read one model and its evidence')
    profile.add_argument('--solver')
    profile.add_argument('--version')
    profile.add_argument('--model')
    selection = caps.add_parser('select-turbulence', help='Match flow, target and planned wall treatment')
    selection.add_argument('requirement', help='UTF-8 JSON file, see examples/cli-v01/turbulence-selection')
    selection.add_argument('--solver')
    selection.add_argument('--version')
    selection.add_argument('--brief', action='store_true', help='Best matches by solver with deduplicated references')
    cap_query = caps.add_parser('query')
    cap_query.add_argument('--output', required=True)
    cap_query.add_argument('--condition', action='append', default=[])
    cap_query.add_argument('--solver')
    cap_query.add_argument('--version')
    cap_show = caps.add_parser('show')
    cap_show.add_argument('id')
    cap_build = caps.add_parser('build')
    cap_build.add_argument('--source', required=True)
    cap_build.add_argument('--output', required=True)
    run = sub.add_parser('run')
    run.add_argument('--project', required=True)
    run.add_argument('--cwd', required=True)
    run.add_argument('--background', action='store_true')
    run.add_argument('--cores', type=int, default=1)
    run.add_argument('--memory-gib', type=float)
    run.add_argument('--wall-seconds', type=float)
    run.add_argument('--input', action='append', default=[])
    run.add_argument('command', nargs=argparse.REMAINDER)
    for name in ('status', 'stop', 'show', 'report'):
        item = sub.add_parser(name)
        item.add_argument('--project', required=True)
        item.add_argument('--rid', required=True)
        if name == 'report':
            item.add_argument('--output')
            item.add_argument('--plots', action='store_true', help='Generate recorded history charts')
            item.add_argument('--velocity-time', help='Explicit OpenFOAM U time directory; uses this run input snapshot')
            item.add_argument('--field-case', help='Explicit project-relative retained case; association with run is unverified')
            item.add_argument('--field-axis', choices=('x', 'y', 'z'), help='Section axis; default shortest mesh dimension')
            item.add_argument('--field-position', type=float, help='Section position in native mesh coordinates')
        if name in ('status', 'show'):
            item.add_argument('--full', action='store_true', help='Print the complete machine-readable result')
    numeric = sub.add_parser('results', help='Query this project numeric results')
    numeric.add_argument('--project', required=True)
    numeric.add_argument('--rid')
    numeric.add_argument('--quantity')
    check = sub.add_parser('self-check', help='List observed facts and missing items without a pass/fail decision')
    check.add_argument('--project', required=True)
    check.add_argument('--rid', action='append', required=True)
    check.add_argument('--facts', help='Project-relative JSON with user-declared assumptions, forces and grid comparison')
    check.add_argument('--monitor', help='Project-relative two-column time/value table')
    check.add_argument('--absolute-pressure', help='Project-relative field explicitly declared as absolute pressure in Pa')
    wait = sub.add_parser('wait', help='Block until the run ends or a stated condition is seen; print a short summary')
    wait.add_argument('--full', action='store_true', help='Print the complete machine-readable result')
    wait.add_argument('--project', required=True)
    wait.add_argument('--rid', required=True)
    wait.add_argument('--timeout', type=float, help='Seconds to wait before returning while the run continues')
    wait.add_argument('--residual-below', type=float, help='Return when every latest initial residual is below this value')
    wait.add_argument('--then-stop', action='store_true', help='Request stop when the residual target is met')
    wait.add_argument('--stop-on-divergence', action='store_true', help='Request stop when the log shows a fatal message')
    monitor = sub.add_parser('monitor', help='Serve a read-only residual and monitor page on 127.0.0.1')
    monitor.add_argument('--project', required=True)
    monitor.add_argument('--port', type=int, default=8790)
    table = sub.add_parser('table', help='Collect end-of-run values of several runs into one CSV')
    table.add_argument('--project', required=True)
    table.add_argument('--rid', action='append', required=True, help='Repeat for each run, in column order')
    table.add_argument('--baseline', help='Run to compare the others against')
    table.add_argument('--label', action='append', default=[], help='rid=text shown in the column header; repeatable')
    table.add_argument('--window', type=int, default=10, help='Number of final samples used for mean and range')
    table.add_argument('--output', default='report/run-table.csv', help='Project-relative .csv path')
    table.add_argument('--full', action='store_true', help='Print the complete machine-readable result')
    diff = sub.add_parser('diff', help='Show what differed between the starting inputs of two runs')
    diff.add_argument('--project', required=True)
    diff.add_argument('--rid', action='append', required=True, help='Give exactly two runs')
    diff.add_argument('--full', action='store_true', help='Print the complete machine-readable result')
    worker = sub.add_parser('_controller')
    worker.add_argument('--project', required=True)
    worker.add_argument('--rid', required=True)
    worker.add_argument('--exe', required=True)
    args = parser.parse_args(argv)
    if args.action == 'fin':
        try:
            from .fin_conduction import build_fin_input, read_fin_dat
            if args.fin_action == 'build':
                model = json.loads(args.model.read_text(encoding='utf-8-sig'))
                if not isinstance(model, dict):
                    raise ValueError('The model must be a JSON object')
                if args.deck.exists():
                    raise FileExistsError(f'Deck already exists: {args.deck}')
                result = build_fin_input(model, args.deck)
            else:
                result = read_fin_dat(args.deck, args.dat, args.positions_m)
            print(json.dumps(result, ensure_ascii=False, allow_nan=False))
            return 0
        except ModuleNotFoundError as error:
            if error.name != 'numpy':
                raise
            print(json.dumps({'status': 'missing_dependency',
                              'message': 'Install the fin extra from this release with python -m pip install ".[fin]"'}))
            return 1
        except (OSError, KeyError, TypeError, ArithmeticError, ValueError) as error:
            print(json.dumps({'status': 'invalid', 'message': str(error)}, ensure_ascii=False))
            return 1
    if args.action == 'capabilities':
        from . import capability_kb
        try:
            if args.capability_action == 'build':
                capability_kb.build(args.source, args.output)
                result = {'status': 'built', 'output': args.output}
            elif args.capability_action == 'list':
                result = capability_kb.inventory(args.solver)
            elif args.capability_action == 'paper-cases':
                from contextlib import closing
                from . import paper_cases
                with closing(capability_kb.connect()) as db:
                    result = paper_cases.query(db, software=args.software, target=args.target,
                                              identifier=args.id, doi=args.doi, industry=args.industry, term=args.term)
            elif args.capability_action in ('select-models', 'model-profiles', 'phase-support', 'select-chains', 'chain-inventory', 'target-inventory', 'target-schema'):
                from contextlib import closing
                from . import device_chains, model_selection
                with closing(capability_kb.connect()) as db:
                    if args.capability_action == 'target-schema':
                        from .selection_vocabulary import schema
                        result = schema(db)
                    elif args.capability_action == 'target-inventory':
                        from .target_selection import inventory
                        result = inventory(db, args.solver, args.version, args.target)
                    elif args.capability_action == 'chain-inventory':
                        result = device_chains.inventory(db, args.solver, args.version, args.chain)
                    elif args.capability_action == 'phase-support':
                        result = model_selection.phase_support(db, args.solver, args.version, args.phenomenon, args.status)
                    elif args.capability_action == 'model-profiles':
                        result = model_selection.profiles(db, args.domain, args.solver, args.version, args.model)
                    else:
                        from .target_cli import run as select_requirement
                        result = select_requirement(db, args)
            elif args.capability_action in ('turbulence-profiles', 'select-turbulence'):
                from contextlib import closing
                from . import turbulence
                with closing(capability_kb.connect()) as db:
                    if args.capability_action == 'turbulence-profiles':
                        result = turbulence.profiles(db, args.solver, args.version, args.model)
                    else:
                        request = json.loads(Path(args.requirement).read_text(encoding='utf-8-sig'))
                        result = turbulence.select(db, request, args.solver, args.version)
                        if args.brief:
                            result = turbulence.brief(result)
            elif args.capability_action == 'feature-summary':
                result = capability_kb.feature_summary(args.solver, by_index=args.by_index)
            elif args.capability_action == 'features':
                result = capability_kb.find_features(args.solver, term=args.term, status=args.status,
                    capability=args.capability, index=args.index, limit=args.limit, offset=args.offset)
            elif args.capability_action == 'modules':
                result = capability_kb.manual_inventory(args.solver)
            elif args.capability_action == 'gaps':
                result = capability_kb.assessed_gaps(args.solver, status=args.status)
            elif args.capability_action == 'couplings':
                result = capability_kb.field_couplings(args.solver, field=args.field, implementation=args.implementation)
            elif args.capability_action == 'show':
                result = capability_kb.show(args.id)
            else:
                result = [{k: c[k] for k in ('id', 'solver', 'version', 'scope')} for c in capability_kb.query(args.output, args.condition, args.solver, args.version)]
        except (OSError, ValueError, TypeError, KeyError, OverflowError, __import__('sqlite3').Error) as error:
            print(json.dumps({'status': 'invalid', 'message': str(error)}, ensure_ascii=False))
            return 1
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.action == 'prepare-strip':
        from . import strip_control
        try:
            request = json.loads(Path(args.requirement).read_text(encoding='utf-8-sig'))
            if not isinstance(request, dict):
                raise ValueError('strip requirement must be an object')
            result = strip_control.prepare(request, args.confirm, args.output)
        except (OSError, ValueError, TypeError, OverflowError) as error:
            print(json.dumps({'status': 'invalid', 'message': str(error)}, ensure_ascii=False))
            return 1
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result['status'] == 'prepared' else 1
    if args.action == 'route':
        from .physics_route import CATALOG, FIELDS, evaluate
        if args.catalog:
            print(json.dumps(CATALOG, ensure_ascii=True, indent=2))
            return 0
        if not args.requirement:
            parser.error('route requires a requirement file or --catalog')
        try:
            requirement = json.loads(Path(args.requirement).read_text(encoding='utf-8-sig'))
            text_only = isinstance(requirement, dict) and not any(k in requirement for k in FIELDS)
            if text_only:
                requirement = {**requirement, **{k: [] for k in FIELDS}}
            result = evaluate(requirement)
            endpoint = os.environ.get("NAVIFIELD_RESEARCH_AGENT_URL")
            if not (text_only or args.brief or args.emit_gaps or args.with_evidence or args.research_cache or endpoint):
                from contextlib import closing
                from . import capability_kb, chain_intake
                with closing(capability_kb.connect()) as db:
                    plans = chain_intake.discover(db, requirement, requirement.get('solver'), requirement.get('version'))
                if plans:
                    result['engineering_plans'] = plans
            if text_only or 'strip' in requirement or args.brief or args.emit_gaps or args.with_evidence or args.research_cache or endpoint:
                from . import capability_kb, public_research
                findings = json.loads(Path(args.with_evidence).read_text(encoding='utf-8-sig')) if args.with_evidence else None
                analysis = capability_kb.analyze(requirement, findings=findings)
                cache = args.research_cache or (public_research.cache_directory() if endpoint else None)
                hits = []
                combined = findings['findings'] if findings else []
                if cache:
                    if findings:
                        capability_kb.store_cache(cache, combined)
                    cached = capability_kb.load_cache(cache, requirement, analysis)
                    explicit_ids = {f['gap_id'] for f in combined}
                    hits = [f['gap_id'] for f in cached]
                    combined = combined + [f for f in cached if f['gap_id'] not in explicit_ids]
                if endpoint:
                    incoming, dispatch = public_research.dispatch(endpoint, analysis, combined, cache)
                    incoming_ids = {f['gap_id'] for f in incoming}
                    combined = [f for f in combined if f['gap_id'] not in incoming_ids] + incoming
                if combined:
                    analysis['research_findings'] = capability_kb.bind_findings(
                        analysis['gaps'], {'schema_version': 1, 'findings': combined})
                if args.emit_gaps:
                    answered = {f['gap_id'] for f in combined if capability_kb.cache_eligible(f)}
                    Path(args.emit_gaps).write_text(json.dumps(capability_kb.research_packet(analysis, answered), ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
                if args.brief:
                    result = capability_kb.brief(analysis)
                    if cache:
                        result['cache_hits'] = hits
                else:
                    result['capability_analysis'] = analysis
                if endpoint:
                    result['public_research'] = dispatch
        except (OSError, ValueError, TypeError, KeyError, OverflowError, __import__('sqlite3').Error) as error:
            print(json.dumps({'status': 'invalid', 'message': str(error)}, ensure_ascii=False))
            return 1
        print(json.dumps(result, ensure_ascii=False, separators=(',', ':')) if args.brief else json.dumps(result, ensure_ascii=True, indent=2))
        return 0
    root = Path(args.project).resolve()
    if args.action == '_controller':
        return _controller(root, args.rid, Path(args.exe))
    if args.action == 'wait':
        from . import live_monitor
        summary = live_monitor.wait(root, args.rid, timeout=args.timeout, residual_below=args.residual_below,
                                    stop_on_divergence=args.stop_on_divergence, then_stop=args.then_stop)
        print(json.dumps(summary if args.full else _short_wait(root, args.rid, summary)))
        return live_monitor.wait_exit_code(summary)
    if args.action == 'diff':
        from . import run_diff
        if len(args.rid) != 2:
            parser.error('give --rid exactly twice')
        try:
            result = run_diff.compare(root, *args.rid)
            paths = run_diff.write(root, result)
        except (OSError, ValueError, KeyError) as error:
            print(json.dumps({'status': 'failed', 'message': str(error)[:300]}))
            return 1
        print(json.dumps(result if args.full else run_diff.preview(result, *paths), ensure_ascii=False))
        return 0
    if args.action == 'table':
        from . import run_table
        if args.window < 1:
            parser.error('--window must be positive')
        labels = dict(item.split('=', 1) for item in args.label if '=' in item)
        try:
            result = run_table.collect(root, args.rid, baseline=args.baseline, labels=labels, window=args.window)
            paths = run_table.write(root, result, args.output)
        except (OSError, ValueError, KeyError) as error:
            print(json.dumps({'status': 'failed', 'message': str(error)[:300]}))
            return 1
        print(json.dumps(result if args.full else run_table.preview(result, *paths), ensure_ascii=False))
        return 0
    if args.action == 'monitor':
        from . import live_monitor
        return live_monitor.serve(root, port=args.port)
    if args.action == 'self-check':
        from .delivery_summary import generate
        output = generate(root, args.rid, facts_path=args.facts, monitor=args.monitor,
                          absolute_pressure=args.absolute_pressure)
        print(json.dumps({'summary': output}))
        return 0
    if args.action == 'run':
        command = args.command[1:] if args.command and args.command[0] == '--' else args.command
        if not command or args.cores < 1:
            parser.error('a command and positive core count are required')
        executable = shutil.which(command[0])
        if not executable:
            parser.error('executable was not found')
        executable = Path(executable).resolve()
        inputs = list(args.input)
        if executable.stem.lower().startswith('python') and command[1:]:
            for value in command[1:]:
                if value in ('-c', '-m'):
                    break
                if value.lower().endswith('.py'):
                    candidate = Path(value)
                    candidate = (candidate if candidate.is_absolute() else root / args.cwd / candidate).resolve()
                    if candidate.is_file() and candidate.is_relative_to(root):
                        relative = candidate.relative_to(root).as_posix()
                        if relative not in inputs:
                            inputs.append(relative)
                    break
        resource = {'cores': args.cores}
        if args.memory_gib is not None:
            resource['memory_bytes'] = int(args.memory_gib * 1024**3)
        if args.wall_seconds is not None:
            resource['wall_seconds'] = args.wall_seconds
        with execution._case_writer(root, root):
            project.open_project(root)
            result = execution.register_case(root, {'path': args.cwd})
            if result['status'] != 'ok':
                raise ValueError(result['failure']['message'])
            prepared = execution.prepare_run(root, {'case_id': result['data']['case_id'], 'toolchain': 'local',
                'program': executable.name, 'argv': command[1:], 'resources': resource, 'purpose': 'tool',
                'cli_inputs': inputs, 'original_request': {'executable': command[0],
                    'resolved_executable': str(executable), 'argv': command[1:]}})
        if prepared['status'] != 'ok':
            raise ValueError(prepared['failure']['message'])
        rid = prepared['data']['run_id']
        print(rid, flush=True)
        if not args.background:
            return _controller(root, rid, executable)
        folder = _location(root, rid)
        if os.name == 'nt':
            _windows_background(root, rid, executable, folder)
            return 0
        options = {'stdin': subprocess.DEVNULL, 'cwd': str(root), 'close_fds': True}
        env = dict(os.environ)
        env['PYTHONPATH'] = str(Path(__file__).resolve().parents[len(__package__.split('.'))]) + os.pathsep + env.get('PYTHONPATH', '')
        options['env'] = env
        options['start_new_session'] = True
        with (folder := _location(root, rid)).joinpath('controller.log').open('ab') as log:
            subprocess.Popen([sys.executable, '-m', __package__ + '.local_cli', '_controller',
                              '--project', str(root), '--rid', rid, '--exe', str(executable)],
                             stdout=log, stderr=log, **options)
        return 0
    if args.action == 'results':
        from .strip_results import query
        if args.rid is not None:
            project.read_run(root, args.rid)
        print(json.dumps({'results': query(root, args.rid, args.quantity)}, ensure_ascii=False))
        return 0
    folder = _location(root, args.rid)
    if args.action == 'stop':
        (folder / 'stop.request').touch()
        print('stop requested')
        return 0
    if args.action == 'show':
        record_path = f'.navifield/records/{args.rid}.json'
        record = json.loads(project.resolve_path(root, record_path).read_text(encoding='utf-8'))
        if args.full:
            print(json.dumps(record))
        else:
            resources = record.get('resources') or {}
            print(json.dumps({'run_id': args.rid, 'state': resources.get('termination'),
                              'exit_code': resources.get('exit_code'), 'cores': resources.get('cores'),
                              'wall_seconds': resources.get('wall_seconds'), 'details': record_path}))
        return 0
    if args.action == 'report':
        if args.velocity_time is not None:
            if args.plots or args.output:
                parser.error('--velocity-time cannot be combined with --plots or --output')
            output_stream = sys.stdout
            try:
                from .cli_native_field import generate as field_report
                result = field_report(root, args.rid, args.velocity_time, field_case=args.field_case,
                                      axis=args.field_axis, position=args.field_position)
            except (RuntimeError, ValueError, OSError, ImportError) as error:
                print(json.dumps({'status': 'failed', 'message': str(error)}), file=output_stream, flush=True)
                return 1
            print(json.dumps(result), file=output_stream, flush=True)
            return 0
        if args.field_case is not None or args.field_axis is not None or args.field_position is not None:
            parser.error('--field-case, --field-axis and --field-position require --velocity-time')
        if args.plots:
            from .cli_plots import generate
            if args.output:
                parser.error('--output cannot be combined with --plots')
            try:
                output = generate(root, args.rid)
            except (RuntimeError, ValueError, OSError) as error:
                print(json.dumps({'status': 'failed', 'message': str(error)}))
                return 1
            print(json.dumps({'report': output}))
            return 0
        from .postprocess import EngineeringReport, ReportParagraph, ReportSection, ReportTable, _html
        record = json.loads((root / '.navifield' / 'records' / (args.rid + '.json')).read_text(encoding='utf-8'))
        output = project.resolve_path(root, args.output or f'report/{args.rid}/index.html')
        output.parent.mkdir(parents=True, exist_ok=True)
        inputs = record.get('input_snapshot') or {}
        entries = [('Run', args.rid), ('Command', json.dumps(record.get('original_request'), ensure_ascii=False)[:2000]),
                   ('Termination', str(record['resources'].get('termination'))),
                   ('Exit code', str(record['resources'].get('exit_code'))),
                   ('Wall seconds', str(record['resources'].get('wall_seconds'))),
                   ('Inputs', f"{inputs.get('path')}; copied {len(inputs.get('copied', []))}, skipped {len(inputs.get('skipped', []))}"),
                   ('Log', f'result/{args.rid}/native.log')]
        sections = [
            ReportSection(key='run', title='Execution', blocks=(ReportTable(columns=('Item', 'Value'), rows=tuple(entries)),)),
            ReportSection(key='scope', title='Scope', blocks=(ReportParagraph(text='This report records execution; it does not establish physical validation.'),)),
        ]
        numeric = record.get('numeric_result')
        if numeric:
            if numeric.get('status') == 'rejected':
                sections.append(ReportSection(key='numeric-result', title='Numeric result', blocks=(
                    ReportParagraph(text='Result not indexed: ' + str(numeric.get('reason', 'unknown'))),)))
            else:
                rows = [(item['name'], str(item['value']), item['unit'],
                         '' if item.get('x_m') is None else str(item['x_m']))
                        for item in numeric.get('quantities', [])]
                sections.append(ReportSection(key='numeric-result', title='Numeric result', blocks=(
                    ReportTable(columns=('Quantity', 'Value', 'Unit', 'Position x [m]'), rows=tuple(rows)),
                    ReportParagraph(text='Model scope: ' + str(numeric.get('scope', ''))),
                    ReportParagraph(text='Archived result: ' + str(numeric.get('result_path', ''))),
                    ReportParagraph(text='Diagnostics (not engineering acceptance): ' +
                                    json.dumps(numeric.get('diagnostics', {}), ensure_ascii=False)),
                )))
        document = EngineeringReport(summary='Local execution record', sections=tuple(sections))
        output.write_text(_html(document, root, output, self_contained=True, title='NaviField execution record'), encoding='utf-8')
        print(json.dumps({'report': output.relative_to(root).as_posix()}))
        return 0
    from .live_monitor import observed_status
    result = observed_status(root, args.rid)
    if args.full:
        print(json.dumps(result))
    elif result.get('status') != 'ok':
        failure = result.get('failure') or {}
        print(json.dumps({'status': result.get('status'), 'failure': {
            'code': str(failure.get('code', 'unknown'))[:120],
            'message': str(failure.get('message', ''))[:300]},
            'details': f'.navifield/runs/{args.rid}/manifest.json',
            'full_output': 'status --full'}))
    else:
        data = result.get('data') or {}
        short = {key: data.get(key) for key in ('run_id', 'rid', 'state', 'exit_code',
                 'started_at', 'finished_at', 'log_path')}
        short.update(findings_count=len(result.get('findings') or []),
                     artifacts_count=len(result.get('artifacts') or []),
                     details=f'.navifield/runs/{args.rid}/manifest.json',
                     record=f'.navifield/records/{args.rid}.json', full_output='status --full')
        print(json.dumps({'status': result['status'], 'data': short}))
    return 0 if result.get('status') == 'ok' else 1


if __name__ == '__main__':
    raise SystemExit(main())
