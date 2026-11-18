"""Private local execution adapter inside the existing JobManager worker."""
from __future__ import annotations

import os
import subprocess
import threading

import psutil

from .job_manager import JobOwner

_current = threading.local()


class _Cancelled(Exception):
    def __init__(self, exit_code=None, native_started=False):
        self.exit_code = exit_code
        self.native_started = native_started
        self.result = ([], [], exit_code)



def native_step(executable, args, *, case, env, output, clean=None, root=None,
                raw_output=None, on_started=None):
    """Launch one step as a descendant of the existing worker/process owner."""
    handle = getattr(_current, 'handle', None)
    if handle is None:
        process = subprocess.Popen([str(executable), *args], cwd=case, env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, shell=False)
    else:
        with handle.lock:
            if handle.cancel_requested:
                raise _Cancelled()
            process = subprocess.Popen([str(executable), *args], cwd=case, env=env,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, shell=False)
            handle.process_active = True
            handle.process = process
            try:
                handle.process_identity = (process.pid, psutil.Process(process.pid).create_time())
            except (psutil.Error, OSError):
                handle.process_identity = None
    if on_started is not None:
        on_started()
    for line in process.stdout:
        if handle is not None and handle.context.get('on_native_line'):
            handle.context['on_native_line'](line)
        if raw_output is not None:
            raw_output.write(line)
            raw_output.flush()
        output.write(line if clean is None else clean(line.decode('utf-8', errors='replace'), root))
        output.flush()
    code = process.wait()
    if handle is not None:
        with handle.lock:
            handle.process_identity = None
            handle.process_active = False
            handle.process = None
            cancelled = handle.cancel_requested
        if cancelled:
            raise _Cancelled(code, native_started=True)
    return code


class _LocalHandle:
    def __init__(self, context):
        self.context = context
        self.state = 'queued'
        self.result = None
        self.error = None
        self.done = threading.Event()
        self.lock = threading.Lock()
        self.process_identity = None
        self.process_active = False
        self.process = None
        self.cancel_requested = False
        self.cancel_reason = None

    def wait(self):
        self.done.wait()


class LocalBackend:
    """Observe a single in-worker sequence; process ownership stays with JobManager."""
    def start(self, context):
        handle = _LocalHandle(context)

        def run():
            with handle.lock:
                handle.state = 'running'
            _current.handle = handle
            try:
                result = context['run_sequence']()
                with handle.lock:
                    handle.result = result
                    handle.state = ('cancelled' if handle.cancel_requested else
                                    'completed' if result[2] == 0 else 'failed')
            except _Cancelled as error:
                with handle.lock:
                    handle.result = error.result
                    handle.state = 'cancelled'
            except BaseException as error:
                with handle.lock:
                    handle.error = error
                    handle.state = 'failed'
            finally:
                handle.done.set()

        threading.Thread(target=run, name='local-backend-sequence', daemon=True).start()
        return handle

    def query(self, handle):
        with handle.lock:
            snapshot = {'state': handle.state,
                        'exit_code': None if handle.result is None else handle.result[2],
                        'reason': None if handle.error is None else str(handle.error)}
        observe = handle.context.get('observe')
        if observe is not None:
            snapshot.update(observe())
            # Local handle is authoritative only for sequence lifecycle/exit.
            with handle.lock:
                snapshot['state'] = handle.state if handle.done.is_set() else 'running'
                snapshot['exit_code'] = None if handle.result is None else handle.result[2]
                snapshot['cancel_reason'] = handle.cancel_reason
        return snapshot

    def cancel(self, handle, *, reason):
        with handle.lock:
            if handle.state in ('completed', 'failed', 'cancelled'):
                return {'state': handle.state, 'cancel_requested': False, 'reason': 'Already terminal.'}
            handle.cancel_requested = True
            handle.cancel_reason = reason
            identity = handle.process_identity
            if handle.process_active and identity is None:
                return {'state': handle.state, 'cancel_requested': True, 'cancel_confirmed': False,
                        'reason': 'Native process identity unavailable.'}
            stopped = True if identity is None else JobOwner.terminate_owned_process(*identity)
            if not stopped:
                return {'state': handle.state, 'cancel_requested': True,
                        'cancel_confirmed': False, 'reason': 'Owned process stop not confirmed.'}
        return {**self.query(handle), 'cancel_requested': True,
                'cancel_confirmed': True, 'cancel_reason': reason}

    def fetch(self, handle):
        if not handle.done.is_set():
            return {'state': 'pending', 'paths': [], 'missing': [], 'reason': None, 'result': None}
        if handle.error is not None:
            raise handle.error
        return {'state': 'complete', 'paths': handle.context.get('artifact_paths', lambda: [])(),
                'missing': [], 'reason': None, 'result': handle.result}
