"""Resolve registered tools and construct their native execution environment."""
from __future__ import annotations
import os
from pathlib import Path
import subprocess

from .path_safety import assert_safe_components


def resolve_tool(config, toolchain, program):
    if not isinstance(program,str) or not program or program in ('.','..') or any(c in program for c in ('/','\\',':','\0')):
        raise ValueError('Program must be a name in the registered toolchain.')
    if not isinstance(config,dict):
        raise ValueError('Requested toolchain is not configured.')
    entry=config.get(toolchain)
    if not isinstance(entry,dict):
        raise ValueError('Requested toolchain is not configured.')
    roots=entry.get('roots')
    if not isinstance(roots,list) or not roots:
        raise ValueError('Registered toolchain roots are required.')
    matches=[]
    for root in roots:
        if not isinstance(root,str) or not Path(root).is_absolute():
            raise ValueError('Registered toolchain root must be absolute.')
        assert_safe_components(root)
        base=Path(root).resolve(strict=True)
        for leaf in dict.fromkeys((program,program+'.exe') if os.name=='nt' else (program,)):
            raw=base/leaf
            assert_safe_components(raw)
            candidate=raw.resolve()
            if (candidate.is_relative_to(base) and candidate.is_file()
                    and (os.name=='nt' or os.access(candidate,os.X_OK)) and candidate not in matches):
                matches.append(candidate)
    if len(matches)!=1:
        raise ValueError('Program is missing or ambiguous in the registered toolchain.')
    return matches[0],entry


def build_environment(entry, *, base=None, environment_script=None, step=None, threads=None):
    if not isinstance(entry,dict):
        raise ValueError('Invalid toolchain environment.')
    env=dict(os.environ if base is None else base)
    script=environment_script if environment_script is not None else entry.get('environment_script')
    if script is not None and script != '':
        if os.name!='posix':
            raise ValueError('Environment scripts require a POSIX target.')
        if not isinstance(script,str) or not script or '\0' in script:
            raise ValueError('Invalid environment script.')
        try:
            loaded=subprocess.run(['bash','-c','source "$1" && env -0','bash',script],
                                  env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=True)
        except (OSError,subprocess.CalledProcessError) as error:
            raise ValueError('Could not load configured toolchain environment.') from error
        for item in loaded.stdout.split(b'\0'):
            if not item: continue
            key,separator,value=item.partition(b'=')
            if not separator or not key:
                raise ValueError('Invalid output from configured environment script.')
            env[os.fsdecode(key)]=os.fsdecode(value)
    for values in (entry.get('environment',{}),step or {},threads or {}):
        if not isinstance(values,dict) or any(not isinstance(k,str) or not isinstance(v,str) or not k or '=' in k or '\0' in k or '\0' in v for k,v in values.items()):
            raise ValueError('Toolchain environment must contain string values.')
        env.update(values)
    return env


def resolve_launcher(entry, name):
    launchers=entry.get('mpi_launchers',{}) if isinstance(entry,dict) else {}
    item=launchers.get(name) if isinstance(launchers,dict) else None
    if not isinstance(item,dict):
        raise ValueError('Requested MPI launcher is not configured.')
    executable=item.get('executable'); flag=item.get('count_flag')
    if (not isinstance(executable,str) or not Path(executable).is_absolute()
            or not isinstance(flag,str) or not flag or '\0' in flag):
        raise ValueError('Invalid registered MPI launcher.')
    assert_safe_components(executable)
    path=Path(executable).resolve(strict=True)
    if not path.is_file() or (os.name!='nt' and not os.access(path,os.X_OK)):
        raise ValueError('Registered MPI launcher is unavailable.')
    return path,flag
