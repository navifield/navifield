@echo off
rem Call from cmd.exe after activating the NaviField Python environment.
if not defined NAVIFIELD_PYTHON if defined VIRTUAL_ENV set "NAVIFIELD_PYTHON=%VIRTUAL_ENV%\Scripts\python.exe"
if not defined NAVIFIELD_PYTHON set "NAVIFIELD_PYTHON=python"
if defined NAVIFIELD_CCX_BIN if not exist "%NAVIFIELD_CCX_BIN%\ccx.exe" goto :missingccx
if defined NAVIFIELD_MPI_BIN if not exist "%NAVIFIELD_MPI_BIN%\mpiexec.exe" goto :missingmpi
if defined NAVIFIELD_FOAM_ROOT goto :foam
if defined NAVIFIELD_PARALLEL_BIN goto :missingfoam
goto :activate

:foam
if not exist "%NAVIFIELD_FOAM_ROOT%\etc\controlDict" goto :missingfoam
if not defined NAVIFIELD_FOAM_BIN set "NAVIFIELD_FOAM_BIN=%NAVIFIELD_FOAM_ROOT%\platforms\win64MingwDPInt32Opt\bin"
if not exist "%NAVIFIELD_FOAM_BIN%\simpleFoam.exe" goto :missingfoam
if not defined NAVIFIELD_PARALLEL_BIN goto :foamvars
if exist "%NAVIFIELD_PARALLEL_BIN%\simpleFoam.exe" goto :foamvars
if defined NAVIFIELD_MPI_DLL goto :withdll
"%NAVIFIELD_PYTHON%" "%~dp0prepare-msmpi.py" "%NAVIFIELD_FOAM_BIN%" "%NAVIFIELD_PARALLEL_BIN%"
if errorlevel 1 exit /b 1
goto :foamvars
:withdll
"%NAVIFIELD_PYTHON%" "%~dp0prepare-msmpi.py" "%NAVIFIELD_FOAM_BIN%" "%NAVIFIELD_PARALLEL_BIN%" --mpi-dll "%NAVIFIELD_MPI_DLL%"
if errorlevel 1 exit /b 1
:foamvars
set "WM_PROJECT=OpenFOAM"
for %%D in ("%NAVIFIELD_FOAM_ROOT%") do set "NF_FOAM_NAME=%%~nxD"
if /i "%NF_FOAM_NAME:~0,9%"=="OpenFOAM-" set "WM_PROJECT_VERSION=%NF_FOAM_NAME:~9%"
set "NF_FOAM_NAME="
set "WM_OPTIONS=win64MingwDPInt32Opt"
set "WM_PROJECT_DIR=%NAVIFIELD_FOAM_ROOT%"
set "FOAM_ETC=%WM_PROJECT_DIR%\etc"
set "FOAM_APPBIN=%NAVIFIELD_FOAM_BIN%"
if defined NAVIFIELD_PARALLEL_BIN set "FOAM_APPBIN=%NAVIFIELD_PARALLEL_BIN%"
set "FOAM_LIBBIN=%FOAM_APPBIN%"
:activate
if defined NAVIFIELD_CCX_BIN set "PATH=%NAVIFIELD_CCX_BIN%;%PATH%"
if defined NAVIFIELD_FOAM_ROOT set "PATH=%FOAM_APPBIN%;%PATH%"
if defined NAVIFIELD_MPI_BIN set "PATH=%NAVIFIELD_MPI_BIN%;%PATH%"
if defined VIRTUAL_ENV set "PATH=%VIRTUAL_ENV%\Scripts;%PATH%"
echo Solver paths configured for this command prompt.
exit /b 0
:missingccx
echo NAVIFIELD_CCX_BIN must contain ccx.exe. 1>&2
exit /b 1
:missingmpi
echo NAVIFIELD_MPI_BIN must contain mpiexec.exe. 1>&2
exit /b 1
:missingfoam
echo Set NAVIFIELD_FOAM_ROOT to the MinGW OpenFOAM project and NAVIFIELD_FOAM_BIN to its solver directory. 1>&2
exit /b 1
