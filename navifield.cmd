@echo off
setlocal
if defined NAVIFIELD_PYTHON (set "NF_PY=%NAVIFIELD_PYTHON%") else (set "NF_PY=python")
"%NF_PY%" -I -m navifield.local_cli %*
exit /b %ERRORLEVEL%
