"""Prepare a private OpenFOAM binary directory from the user's MinGW installation."""

import argparse
import os
from pathlib import Path
import shutil
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="OpenFOAM directory containing simpleFoam.exe")
    parser.add_argument("destination", type=Path, help="new directory outside the OpenFOAM installation")
    parser.add_argument("--mpi-dll", type=Path, help="msmpi.dll from the matching Microsoft MPI runtime")
    args = parser.parse_args()
    source = args.source.resolve()
    destination = args.destination.resolve()
    variant = source / "libPstream.dll-msmpi"
    if not (source / "simpleFoam.exe").is_file() or not variant.is_file():
        parser.error("The source must contain simpleFoam.exe and the MS-MPI variant libPstream.dll-msmpi.")
    if args.mpi_dll and (args.mpi_dll.name.lower() != "msmpi.dll" or not args.mpi_dll.is_file()):
        parser.error("--mpi-dll must point to the installed Microsoft MPI runtime file msmpi.dll.")
    if destination.exists() or destination.is_relative_to(source):
        parser.error("Choose a new directory outside the source bin directory.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
        staged = Path(temporary) / "bin"
        shutil.copytree(source, staged)
        shutil.copy2(variant, staged / "libPstream.dll")
        if args.mpi_dll:
            shutil.copy2(args.mpi_dll, staged / "msmpi.dll")
        os.rename(staged, destination)
    print(f"Prepared OpenFOAM MS-MPI binaries in {destination}")


if __name__ == "__main__":
    main()
