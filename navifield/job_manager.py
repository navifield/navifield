"""Single owner for fixed engine workers and their native descendants."""
from __future__ import annotations
import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import psutil
from .resource_limits import ResourceOwner
from .worker_protocol import MAX_FRAME, read_frame, write_frame

def read_response(task, job_id, frame):
    stream=io.BytesIO(frame); result=read_frame(stream)
    if stream.read(1) or result.get('job_id')!=job_id or set(result)!={'job_id','artifact'}:
        raise ValueError('worker result binding invalid')
    artifact=result['artifact']
    if not isinstance(artifact,dict) or set(artifact)!={'path','size','sha256'} or artifact['path']!='response.json':
        raise ValueError('worker response declaration invalid')
    path=task/'response.json'; info=path.lstat()
    if not stat.S_ISREG(info.st_mode) or path.is_symlink() or getattr(info,'st_file_attributes',0)&0x400:
        raise ValueError('worker response is not ordinary')
    if type(artifact['size']) is not int or artifact['size']<0 or artifact['size']>MAX_FRAME or info.st_size!=artifact['size']:
        raise ValueError('worker response size mismatch')
    fd=os.open(path, os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as source:
        current=os.fstat(source.fileno())
        if not stat.S_ISREG(current.st_mode) or (current.st_dev,current.st_ino)!=(info.st_dev,info.st_ino):
            raise ValueError('worker response identity changed')
        data=source.read(MAX_FRAME+1)
        after=os.fstat(source.fileno())
        if (after.st_size,after.st_mtime_ns)!=(current.st_size,current.st_mtime_ns):
            raise ValueError('worker response changed while reading')
    if len(data)!=artifact['size'] or hashlib.sha256(data).hexdigest()!=artifact['sha256']:
        raise ValueError('worker response content mismatch')
    response=json.loads(data)
    if (not isinstance(response,dict) or response.get('status') not in ('ok','failed','needs_decision')
            or not isinstance(response.get('data'),dict)
            or not isinstance(response.get('findings'),list)
            or not isinstance(response.get('artifacts'),list)):
        raise ValueError('worker response is not a valid envelope')
    return response



@dataclass
class Outcome:
    status: str
    response: dict | None = None
    exit_code: int | None = None
    error: str | None = None
    resource_observation: dict | None = None


@dataclass
class _State:
    done: threading.Event = field(default_factory=threading.Event)
    lock: threading.RLock = field(default_factory=threading.RLock)
    process: object = None
    resources: object = None
    cancelled: bool = False
    outcome: Outcome | None = None


class JobOwner:
    def __init__(self, root, logger, on_pending=None, worker_module=None):
        self.root=Path(root); self.root.mkdir(parents=True,exist_ok=True)
        self.logger=logger; self.states={}; self.lock=threading.Lock()
        self.on_pending=on_pending; self.worker_module=worker_module

    def _read_owner_state(self, job_id):
        path=self.root/job_id/'owner.json'
        try:
            info=path.lstat()
        except FileNotFoundError:
            return 'missing', None
        except OSError:
            return 'unknown', None
        if not stat.S_ISREG(info.st_mode) or path.is_symlink() or getattr(info,'st_file_attributes',0)&0x400 or info.st_size>65536:
            return 'unknown', None
        try:
            record=json.loads(path.read_text(encoding='utf8'))
        except (OSError,ValueError,UnicodeError):
            return 'unknown', None
        if (not isinstance(record,dict) or record.get('job_id')!=job_id or
                record.get('phase') not in ('starting','running','cleanup_pending','empty')):
            return 'unknown', None
        return record['phase'],record

    def owner_phase(self, job_id):
        return self._read_owner_state(job_id)[0]

    def _owner_state(self, job_id, **changes):
        path=self.root/job_id/'owner.json'
        phase,current=self._read_owner_state(job_id)
        if phase=='unknown':
            raise ValueError('invalid owner state')
        if phase=='missing': current={'job_id':job_id}
        current.update(changes)
        temp=path.with_name('owner.json.tmp')
        with temp.open('w',encoding='utf8') as stream:
            json.dump(current,stream,ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp,path)

    def cleanup_unconfirmed(self, job_id):
        return self.owner_phase(job_id) not in ('missing','empty')

    def submit(self, job_id, payload, *, on_started, wall_seconds):
        with self.lock:
            if job_id in self.states: raise ValueError('duplicate job')
            state=_State(); self.states[job_id]=state
        threading.Thread(target=self._run,args=(state,job_id,payload,on_started,wall_seconds),daemon=True).start()

    def wait(self, job_id):
        state=self.states[job_id]; state.done.wait(); return state.outcome

    def cancel(self, job_id):
        state=self.states[job_id]
        with state.lock:
            if state.done.is_set(): return
            state.cancelled=True
            if state.process and state.resources:
                state.resources.terminate(state.process)

    @staticmethod
    def terminate_owned_process(pid, created):
        try:
            p=psutil.Process(pid)
            if abs(p.create_time()-created)>=.001: return False
            children=p.children(recursive=True)
            for child in reversed(children):
                try: child.kill()
                except psutil.NoSuchProcess: pass
            try: p.kill()
            except psutil.NoSuchProcess: pass
            _, alive=psutil.wait_procs(children+[p],timeout=5)
            return not alive
        except (psutil.Error,OSError): return False

    def _run(self,state,job_id,payload,on_started,wall_seconds):
        process=None; owner=None; status='failed'; error=None; response=None; observed=None
        try:
            task=self.root/job_id
            task.mkdir(exist_ok=False)
            limits=payload['resources'] if payload.get('location','local')=='local' else {'cores':1,'memory_bytes':None,'wall_seconds':None}
            owner=(ResourceOwner(limits,task,worker_module=self.worker_module) if self.worker_module
                   else ResourceOwner(limits,task))
            self._owner_state(job_id, phase='starting', job_name=owner.name, pid=None, created=None)
            payload={**payload,'resource_policy':{'cores':limits['cores'],'commit_bytes':limits.get('memory_bytes'),
                     'active_processes':None,'wall_seconds':limits.get('wall_seconds'),'sampled_disk_bytes':None,
                     'sampled_entries':None,'deadline_unix_seconds':None}}
            frame=io.BytesIO(); write_frame(frame,{'job_id':job_id,'payload':payload})
            deadline = None if wall_seconds is None else time.monotonic() + wall_seconds
            with state.lock:
                if state.cancelled: raise InterruptedError('cancelled before start')
                process=subprocess.Popen(owner.worker_argv(),cwd=task,
                    stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,shell=False,close_fds=True,
                    env=owner.environment(),**owner.spawn_options())
                state.process=process; state.resources=owner
                owner.attach_and_resume(process)
                created=psutil.Process(process.pid).create_time()
                self._owner_state(job_id, phase='running', pid=process.pid, created=created)
            on_started(process.pid,created)
            chunks={'out':bytearray(),'err':bytearray()}; overflow=threading.Event()
            def drain(pipe,key,limit):
                while True:
                    block=pipe.read(65536)
                    if not block: break
                    room=limit-len(chunks[key])
                    if room>0: chunks[key].extend(block[:room])
                    if key=='out' and len(block)>room and not overflow.is_set():
                        overflow.set()
                        try: owner.terminate(process)
                        except (OSError, ValueError): self.logger.exception('Overflow termination failed')
            readers=[threading.Thread(target=drain,args=(process.stdout,'out',MAX_FRAME+4),daemon=True),
                     threading.Thread(target=drain,args=(process.stderr,'err',65536),daemon=True)]
            for t in readers:t.start()
            write_errors=[]
            def send():
                try:
                    process.stdin.write(frame.getvalue()); process.stdin.close()
                except (OSError, ValueError) as exc:
                    write_errors.append(exc)
            sender=threading.Thread(target=send,daemon=True); sender.start()
            remaining=None if deadline is None else max(0, deadline-time.monotonic())
            try: process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                status='timed_out'; owner.terminate(process); process.wait(); raise
            sender.join(timeout=5)
            for t in readers:t.join(timeout=5)
            self.logger.info('Worker returncode=%s stderr=%s',process.returncode,bytes(chunks['err']).decode('utf8',errors='replace'))
            with state.lock:
                if state.cancelled: raise InterruptedError('cancelled')
            if process.returncode!=0 or overflow.is_set() or sender.is_alive() or write_errors or any(t.is_alive() for t in readers):
                raise ValueError('worker exited or returned unbounded/incomplete output')
            response=read_response(task, job_id, bytes(chunks['out']))
            status='complete'
        except InterruptedError as exc:
            status='cancelled'; error=str(exc)
        except subprocess.TimeoutExpired as exc:
            error=str(exc)
        except BaseException as exc:
            error=f'{type(exc).__name__}: {exc}'; self.logger.exception('Worker owner failed')
            with state.lock:
                if state.cancelled: status='cancelled'
        finally:
            pending=False
            def mark_pending():
                nonlocal pending
                if pending: return
                pending=True
                try: self._owner_state(job_id, phase='cleanup_pending')
                except (OSError, ValueError): self.logger.exception('Owner pending state could not be saved')
                if self.on_pending:
                    try: self.on_pending(job_id)
                    except Exception: self.logger.exception('Pending state callback failed')
            if process is not None and owner is not None:
                try: owner.terminate(process)
                except (OSError,ValueError):
                    self.logger.exception('Worker tree termination failed'); mark_pending()
            if process is not None:
                if process.poll() is None:
                    try: process.kill()
                    except OSError:
                        self.logger.exception('Worker termination unavailable'); mark_pending()
                while True:
                    try:
                        process.wait(timeout=5)
                        break
                    except (OSError, subprocess.TimeoutExpired):
                        self.logger.exception('Worker exit unavailable'); mark_pending()
                        time.sleep(1)
            if process is not None and owner is not None:
                while True:
                    try:
                        owner.wait_empty(process)
                        break
                    except (OSError, ValueError, TimeoutError):
                        self.logger.exception('Worker tree empty state unavailable')
                        mark_pending()
                        time.sleep(1)
            if owner is not None:
                try: observed=owner.observation()
                except (OSError, ValueError): self.logger.exception('Resource observation unavailable')
                if (self.root/job_id/'owner.json').exists():
                    while True:
                        try:
                            self._owner_state(job_id, phase='empty')
                            break
                        except (OSError, ValueError):
                            self.logger.exception('Owner empty state could not be saved')
                            if self.on_pending:
                                try: self.on_pending(job_id)
                                except Exception: self.logger.exception('Pending state callback failed')
                            time.sleep(1)
                owner.close()
            with state.lock:
                if state.cancelled:
                    status='cancelled'; response=None
                state.process=None; state.resources=None
                state.outcome=Outcome(status,response,process.returncode if process else None,error,observed)
                state.done.set()
