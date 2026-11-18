"""Shared native step sequence for local and detached execution."""
from pathlib import Path
import math
import re
import time


def run_sequence(case, environment, steps, output, *, native_step, parse_checkmesh, cancelled_error, precheck=None, root=None, on_step=None, on_started=None, on_result=None):
    """Run registered steps, preserving precheck results without quality gating."""
    records, findings = [], []
    if precheck is not None:
        try:
            precheck_log = Path.cwd() / ('precheck-' + str(time.time_ns()) + '.log')
            with precheck_log.open('wb') as private:
                check_exit = native_step(precheck, ['-case', str(case)], case=case, env=environment,
                                          output=output, root=root, raw_output=private, on_started=on_started)
            measured = parse_checkmesh(precheck_log.read_text(encoding='utf8', errors='replace'))
            records.append({'step': 'checkMesh', 'exit_code': check_exit, 'measurements': measured.to_dict()})
            if measured.complete and measured.failed_checks:
                findings.append({'code': 'MESH_QUALITY_WARNING', 'severity': 'warn',
                    'measured': measured.to_dict(), 'limit': None,
                    'message': 'The native mesh check reported a problem; see the retained log.', 'location': None})
            elif not measured.complete or check_exit:
                findings.append({'code': 'MEASUREMENT_UNAVAILABLE', 'severity': 'warn',
                    'measured': {'exit_code': check_exit, 'missing': measured.missing}, 'limit': None,
                    'message': 'The native mesh check did not produce complete measurements.', 'location': None})
        except cancelled_error as error:
            records.append({'step': 'checkMesh', 'exit_code': error.exit_code,
                            'native_started': error.native_started})
            error.result = (records, findings, error.exit_code)
            raise
        except OSError as error:
            records.append({'step': 'checkMesh', 'exit_code': None})
            findings.append({'code': 'MEASUREMENT_UNAVAILABLE', 'severity': 'warn',
                'measured': {'reason': str(error)}, 'limit': None,
                'message': 'The native mesh check could not run.', 'location': None})
    if precheck is not None and on_result is not None:
        on_result(records, findings)
    for stage, executable, args, env in steps:
        if on_step is not None:
            on_step(stage)
        private_path = Path.cwd() / ('native-' + stage + '-' + str(time.time_ns()) + '.log')
        try:
            with private_path.open('wb') as private:
                code = native_step(executable, args, case=case, env=env, output=output, root=root,
                                    raw_output=private, on_started=on_started)
        except cancelled_error as error:
            records.append({'step': stage, 'exit_code': error.exit_code,
                            'native_started': error.native_started})
            error.result = (records, findings, error.exit_code)
            raise
        record = {'step': stage, 'exit_code': code}
        if stage == 'execute':
            with private_path.open('rb') as stream:
                first = stream.read(256 * 1024).decode('latin-1')
            match = re.search(r'(?m)^\s*Create mesh for time\s*=\s*([+\-0-9.eE]+)\s*$', first)
            if match:
                try:
                    observed = float(match.group(1))
                    if math.isfinite(observed):
                        record['actual_start'] = observed
                except ValueError:
                    pass
        records.append(record)
        if on_result is not None:
            on_result(records, findings)
        if code:
            return records, findings, code
    return records, findings, 0



def terminal_state(outcome, cancel_requested):
    """Resolve the outcome exactly at the caller's serialized publication point."""
    return 'cancel_pending' if cancel_requested else outcome
