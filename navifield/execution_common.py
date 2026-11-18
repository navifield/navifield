"""Shared execution snapshot, locking and retained residual publication."""
import contextlib
import csv
import hashlib
import os
from pathlib import Path
import shutil
import time
from . import project


def _snapshot_inputs(root, run):
    """Keep small starting inputs without affecting the outcome of a run."""
    relative = 'model/' + run['run_id'] + '/inputs'
    snapshot = {'path': relative, 'copied': [], 'skipped': []}
    run['input_snapshot'] = snapshot
    failures = []
    try:
        case = project.resolve_path(root, run['execution_case_path'])
        destination = project.resolve_path(root, relative)
        destination.mkdir(parents=True, exist_ok=True)
        def walk_error(error):
            failures.append(str(error))
        for folder, directories, files in os.walk(case, followlinks=False, onerror=walk_error):
            folder = Path(folder)
            directories[:] = [name for name in directories
                              if not (folder / name).is_symlink()
                              and (folder / name).resolve().is_relative_to(case)
                              and (folder / name).resolve() != destination]
            for name in files:
                source = folder / name
                name_rel = source.relative_to(case).as_posix()
                try:
                    if source.is_symlink():
                        snapshot['skipped'].append({'path': name_rel, 'reason': 'link'})
                        continue
                    stat = source.stat()
                    if stat.st_size > 1024 * 1024:
                        snapshot['skipped'].append({'path': name_rel, 'size': stat.st_size,
                                                    'modified_time': stat.st_mtime})
                        continue
                    target = destination / source.relative_to(case)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
                    snapshot['copied'].append(name_rel)
                except OSError as error:
                    failures.append(str(error))
                    snapshot['skipped'].append({'path': name_rel, 'reason': 'copy_failed'})
        for name in run.get('execution_request', {}).get('cli_inputs', []):
            try:
                source = project.resolve_path(root, name)
                if source.is_relative_to(case):
                    continue
                info = source.stat()
                if not source.is_file() or source.is_symlink():
                    raise OSError('declared input is not a regular file')
                if info.st_size > 1024 * 1024:
                    snapshot['skipped'].append({'path': name, 'size': info.st_size, 'modified_time': info.st_mtime})
                    continue
                target = project.resolve_path(root, 'model/' + run['run_id'] + '/declared-inputs/' + name)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                snapshot['copied'].append({'source': name, 'path': target.relative_to(root).as_posix()})
            except (OSError, ValueError, TypeError) as error:
                failures.append(str(error))
                snapshot['skipped'].append({'path': name, 'reason': 'copy_failed'})
    except (OSError, ValueError) as error:
        failures.append(str(error))
    if failures:
        run.setdefault('findings', []).append({'code': 'INPUT_SNAPSHOT_INCOMPLETE', 'severity': 'warn',
            'measured': None, 'limit': None, 'message': 'Some input files could not be copied.', 'location': relative})



@contextlib.contextmanager
def _case_writer(root, case, *, wait=True):
    """Serialize writers of a registered case across API worker processes."""
    name = hashlib.sha256(str(case.resolve()).casefold().encode('utf-8')).hexdigest()[:24]
    path = project.resolve_path(root, '.navifield/cases/' + name + '.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        if os.name == 'nt':
            import msvcrt
            while True:
                try:
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if not wait:
                        raise BlockingIOError('case is busy')
                    time.sleep(.2)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        try:
            yield
        finally:
            if os.name == 'nt':
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)



def _publish_residuals(root, run, log):
    from .foam_readers import ResidualParser
    parser = ResidualParser()
    points = []
    with log.open('rb') as stream:
        while chunk := stream.read(65536):
            points.extend(parser.feed(chunk))
    if not points:
        return
    relative = 'result/' + run['run_id'] + '/residuals.csv'
    with project.resolve_path(root, relative).open('w', encoding='utf8', newline='') as output:
        writer = csv.writer(output)
        writer.writerow(['iteration', 'region', 'equation', 'initial', 'final', 'iterations'])
        writer.writerows((p.axis, p.region, p.equation, p.initial, p.final, p.iterations) for p in points)
    run['residuals_path'] = relative
    run['artifacts'].append({'path': relative, 'kind': 'residual', 'media_type': 'text/csv',
                             'run_id': run['run_id'], 'label': 'Native residual history'})
