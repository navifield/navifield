"""Convert a Gmsh mesh to OpenFOAM using the separately installed utility."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('mesh', type=Path, help='Gmsh .msh file')
    p.add_argument('--case', required=True, type=Path, help='existing OpenFOAM case')
    p.add_argument('--converter', default='gmshToFoam', help='path to gmshToFoam executable')
    p.add_argument('--log', type=Path, help='new log file (default: gmshToFoam.log in the case)')
    a = p.parse_args()
    mesh, case = a.mesh.resolve(), a.case.resolve()
    if not mesh.is_file() or mesh.suffix.lower() != '.msh':
        p.error('mesh must be an existing .msh file')
    if not (case / 'system' / 'controlDict').is_file():
        p.error('case must contain system/controlDict')
    if (case / 'constant' / 'polyMesh').exists():
        p.error('case already contains constant/polyMesh; use a new case')
    converter = shutil.which(a.converter)
    if not converter:
        p.error('gmshToFoam not found; activate OpenFOAM or specify --converter')
    log = a.log.resolve() if a.log else case / 'gmshToFoam.log'
    try:
        with log.open('xb') as stream:
            result = subprocess.run([converter, str(mesh), '-case', str(case)],
                                    stdout=stream, stderr=subprocess.STDOUT, check=False)
    except FileExistsError:
        p.error('log already exists; choose a new --log file')
    except OSError as exc:
        print(json.dumps({'status': 'error', 'message': str(exc), 'log': str(log)}), file=sys.stderr)
        return 1
    print(json.dumps({'status': 'completed' if result.returncode == 0 else 'failed',
                      'exit_code': result.returncode, 'log': str(log)}))
    return result.returncode



if __name__ == '__main__':
    sys.exit(main())
