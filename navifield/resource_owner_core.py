"""OS-owned execution limits, established before the fixed worker resumes."""
from __future__ import annotations
import ctypes
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import psutil


class ResourceOwner:
    def __init__(self, resources, task_dir, worker_module=None):
        self.worker_module = worker_module
        self.cores = resources['cores']
        self.memory = resources.get('memory_bytes')
        self.handle = None
        self.name = None
        self.task_dir = Path(task_dir)
        self.scratch = self.task_dir / 'native-tmp'
        self.scratch.mkdir()
        if os.name == 'nt':
            self._windows_create()
        elif os.name == 'posix':
            if self.memory is not None:
                raise ValueError('aggregate memory limit unavailable on this platform')
            available = sorted(os.sched_getaffinity(0))
            if len(available) < self.cores:
                raise ValueError('requested cores unavailable')
            self.cpus = set(available[:self.cores])
        else:
            raise ValueError('resource enforcement unavailable on this platform')

    def _windows_create(self):
        from ctypes import wintypes as w
        class Basic(ctypes.Structure):
            _fields_ = [('PerProcessUserTimeLimit', ctypes.c_longlong), ('PerJobUserTimeLimit', ctypes.c_longlong),
                        ('LimitFlags', w.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t),
                        ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', w.DWORD),
                        ('Affinity', ctypes.c_size_t), ('PriorityClass', w.DWORD), ('SchedulingClass', w.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in ('ReadOperationCount','WriteOperationCount','OtherOperationCount','ReadTransferCount','WriteTransferCount','OtherTransferCount')]
        class Extended(ctypes.Structure):
            _fields_ = [('BasicLimitInformation', Basic), ('IoInfo', IO), ('ProcessMemoryLimit', ctypes.c_size_t),
                        ('JobMemoryLimit', ctypes.c_size_t), ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]
        class Rate(ctypes.Structure):
            _fields_ = [('ControlFlags', w.DWORD), ('CpuRate', w.DWORD)]
        self._extended, self._rate = Extended, Rate
        k = self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        for name,args,result in [('CreateJobObjectW',[ctypes.c_void_p,w.LPCWSTR],w.HANDLE),
            ('SetInformationJobObject',[w.HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD],w.BOOL),
            ('QueryInformationJobObject',[w.HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD,ctypes.c_void_p],w.BOOL),
            ('AssignProcessToJobObject',[w.HANDLE,w.HANDLE],w.BOOL),('TerminateJobObject',[w.HANDLE,w.UINT],w.BOOL),
            ('CloseHandle',[w.HANDLE],w.BOOL),('GetActiveProcessorCount',[w.WORD],w.DWORD)]:
            f=getattr(k,name); f.argtypes=args; f.restype=result
        total=int(k.GetActiveProcessorCount(0xffff))
        if total < self.cores:
            raise ValueError('requested cores unavailable')
        self.name='Local\\NaviField.OpenCase.'+uuid.uuid4().hex
        ctypes.set_last_error(0)
        self.handle=k.CreateJobObjectW(None,self.name)
        created_error=ctypes.get_last_error()
        if not self.handle: raise OSError(created_error, 'CreateJobObjectW')
        if created_error == 183:
            self.close()
            raise ValueError('Job name collision')
        try:
            info=Extended(); info.BasicLimitInformation.LimitFlags=0x2000 | (0x200 if self.memory is not None else 0)
            if self.memory is not None: info.JobMemoryLimit=self.memory
            rate=Rate(5,max(1,self.cores*10000//total))
            if not k.SetInformationJobObject(self.handle,9,ctypes.byref(info),ctypes.sizeof(info)):
                raise OSError(ctypes.get_last_error(),'Job limits unavailable')
            if not k.SetInformationJobObject(self.handle,15,ctypes.byref(rate),ctypes.sizeof(rate)):
                raise OSError(ctypes.get_last_error(),'Job CPU limit unavailable')
            check=Extended(); actual=Rate()
            if not k.QueryInformationJobObject(self.handle,9,ctypes.byref(check),ctypes.sizeof(check),None) or not k.QueryInformationJobObject(self.handle,15,ctypes.byref(actual),ctypes.sizeof(actual),None):
                raise OSError(ctypes.get_last_error(),'Job limit readback unavailable')
            if check.BasicLimitInformation.LimitFlags != info.BasicLimitInformation.LimitFlags or (self.memory is not None and check.JobMemoryLimit != self.memory) or actual.ControlFlags != 5 or actual.CpuRate != rate.CpuRate:
                raise ValueError('Job limit readback mismatch')
        except BaseException:
            self.close(); raise

    def observation(self):
        if os.name != 'nt':
            return {'cores': self.cores, 'cpus': sorted(self.cpus), 'memory_limit_bytes': None}
        info=self._extended(); rate=self._rate()
        if not self.kernel.QueryInformationJobObject(self.handle,9,ctypes.byref(info),ctypes.sizeof(info),None) or not self.kernel.QueryInformationJobObject(self.handle,15,ctypes.byref(rate),ctypes.sizeof(rate),None):
            raise OSError(ctypes.get_last_error(),'Job limit readback unavailable')
        return {'cores':self.cores, 'memory_limit_bytes':int(info.JobMemoryLimit) if info.BasicLimitInformation.LimitFlags & 0x200 else None,
                'peak_job_commit_bytes':int(info.PeakJobMemoryUsed), 'cpu_rate':int(rate.CpuRate),
                'cpu_rate_flags':int(rate.ControlFlags), 'kill_on_close':bool(info.BasicLimitInformation.LimitFlags & 0x2000)}

    def environment(self):
        env=os.environ.copy()
        env.update(TMP=str(self.scratch), TEMP=str(self.scratch), OMP_NUM_THREADS=str(self.cores),
                   MKL_NUM_THREADS=str(self.cores), OPENBLAS_NUM_THREADS='1', NUMEXPR_NUM_THREADS='1', VTK_SMP_MAX_THREADS='1')
        if self.worker_module:
            package_root = str(Path(__file__).resolve().parents[len(__package__.split('.'))])
            env['PYTHONPATH'] = os.pathsep.join((package_root, env.get('PYTHONPATH', '')))
        if self.name: env['NAVIFIELD_OPEN_CASE_JOB']=self.name
        return env

    def spawn_options(self):
        if os.name=='nt': return {'creationflags':subprocess.CREATE_NEW_PROCESS_GROUP | 0x4}
        return {'start_new_session':True}

    def attach_and_resume(self, process):
        if os.name!='nt': return
        if not self.kernel.AssignProcessToJobObject(self.handle,int(process._handle)):
            raise OSError(ctypes.get_last_error(),'Job assignment failed')
        threads=psutil.Process(process.pid).threads()
        if len(threads)!=1: raise ValueError('suspended worker thread identity unavailable')
        from ctypes import wintypes as w
        k=self.kernel
        k.OpenThread.argtypes=[w.DWORD,w.BOOL,w.DWORD]; k.OpenThread.restype=w.HANDLE
        k.GetProcessIdOfThread.argtypes=[w.HANDLE]; k.GetProcessIdOfThread.restype=w.DWORD
        k.ResumeThread.argtypes=[w.HANDLE]; k.ResumeThread.restype=w.DWORD
        h=k.OpenThread(0x802,False,threads[0].id)
        if not h: raise OSError(ctypes.get_last_error(),'OpenThread failed')
        try:
            if k.GetProcessIdOfThread(h)!=process.pid or k.ResumeThread(h)!=1:
                raise ValueError('suspended worker resume failed')
        finally: k.CloseHandle(h)

    def terminate(self, process):
        if os.name=='nt':
            if self.handle and not self.kernel.TerminateJobObject(self.handle,86):
                raise OSError(ctypes.get_last_error(),'Job termination failed')
        elif process:
            try: os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError: pass

    def wait_empty(self, process, timeout=5):
        deadline=time.monotonic()+timeout
        if os.name == 'nt':
            from ctypes import wintypes as w
            class Accounting(ctypes.Structure):
                _fields_=[(n,ctypes.c_longlong) for n in ('TotalUserTime','TotalKernelTime','ThisPeriodTotalUserTime','ThisPeriodTotalKernelTime')]
                _fields_ += [(n,w.DWORD) for n in ('TotalPageFaultCount','TotalProcesses','ActiveProcesses','TotalTerminatedProcesses')]
            while True:
                row=Accounting()
                if not self.kernel.QueryInformationJobObject(self.handle,1,ctypes.byref(row),ctypes.sizeof(row),None):
                    raise OSError(ctypes.get_last_error(),'Job process accounting unavailable')
                if row.ActiveProcesses == 0: return
                if time.monotonic() >= deadline: raise TimeoutError('owned Job did not become empty')
                time.sleep(.02)
        elif process:
            while True:
                try: os.killpg(process.pid,0)
                except ProcessLookupError: return
                if time.monotonic() >= deadline: raise TimeoutError('owned process group did not become empty')
                time.sleep(.02)

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle); self.handle=None
