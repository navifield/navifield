"""Small core path checks for authorized project and toolchain roots."""
from __future__ import annotations
import os
import stat
from pathlib import Path


def assert_safe_components(path):
    absolute=Path(os.path.abspath(os.fspath(Path(path).expanduser())))
    current=Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current=current/component
        try:
            info=current.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) &
                getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)):
            raise ValueError('path contains a link or reparse point')
        junction=getattr(current,'is_junction',None)
        if junction and junction():
            raise ValueError('path contains a junction')
