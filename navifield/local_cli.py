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
    _write(folder / 'controller.json', {'pid': os.getpid(), 'created': psutil.Process().create_time(), 'state': 'running'})
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
    _write(folder / 'controller.json', {'pid': os.getpid(), 'created': psutil.Process().create_time(),
                                       'state': state['state']})
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
            state = json.loads((folder / 'controller.json').read_text(encoding='utf-8'))
            owner = json.loads((folder / 'owner' / 'local' / 'owner.json').read_text(encoding='utf-8'))
            if (state.get('pid') == pid and state.get('state') in ('completed', 'failed', 'cancelled')
                    and owner.get('phase') == 'empty'
                    and (root / '.navifield' / 'records' / (rid + '.json')).is_file()):
                return
            raise
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                state = json.loads((folder / 'controller.json').read_text(encoding='utf-8'))
                if state.get('pid') == pid and abs(state.get('created', 0) - created) < .001:
                    return
            except (OSError, ValueError, TypeError):
                pass
            if not proc.is_running():
                break
            time.sleep(.05)
    except (IndexError, ValueError, psutil.Error) as error:
        raise RuntimeError('Background controller identity unavailable.') from error
    raise RuntimeError('Background controller did not confirm startup; inspect controller.log.')


def main(argv=None):
    parser = argparse.ArgumentParser(prog='navifield')
    sub = parser.add_subparsers(dest='action', required=True)
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
            item.add_argument('--plots', action='store_true', help='Generate a retained-field report using navifield[plots]')
        if name in ('status', 'show'):
            item.add_argument('--full', action='store_true', help='Print the complete machine-readable result')
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
    worker = sub.add_parser('_controller')
    worker.add_argument('--project', required=True)
    worker.add_argument('--rid', required=True)
    worker.add_argument('--exe', required=True)
    args = parser.parse_args(argv)
    root = Path(args.project).resolve()
    if args.action == '_controller':
        return _controller(root, args.rid, Path(args.exe))
    if args.action == 'wait':
        from . import live_monitor
        summary = live_monitor.wait(root, args.rid, timeout=args.timeout, residual_below=args.residual_below,
                                    stop_on_divergence=args.stop_on_divergence, then_stop=args.then_stop)
        print(json.dumps(summary if args.full else _short_wait(root, args.rid, summary)))
        return live_monitor.wait_exit_code(summary)
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
        document = EngineeringReport(summary='Local execution record', sections=(
            ReportSection(key='run', title='Execution', blocks=(ReportTable(columns=('Item', 'Value'), rows=tuple(entries)),)),
            ReportSection(key='scope', title='Scope', blocks=(ReportParagraph(text='This report records execution; it does not establish physical validation.'),))))
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
