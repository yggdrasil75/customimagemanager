@echo off
rem Start the app (the counterpart of run.sh). A module you enable in
rem Settings -> Modules gets its declared pip deps installed on the next start
rem (--no-auto-install to stop that). Any other argument goes to manager.py
rem (--no-bundle: serve the JS/CSS files one by one, for debugging).
setlocal EnableDelayedExpansion
cd /d "%~dp0"
rem the venv install.bat made, else the Python a portable release ships in python\
set PYEXE=venv\Scripts\python.exe
if not exist %PYEXE% set PYEXE=python\python.exe
if not exist %PYEXE% (
    echo run install.bat first >&2
    pause
    exit /b 1
)
rem cjxl, ffmpeg, tailwindcss and friends that install.bat put in tools\
set "PATH=%~dp0tools;%PATH%"

set AUTO=1
set ARGS=
for %%a in (%*) do (
    if /i "%%a"=="--no-auto-install" ( set AUTO=0 ) else ( set ARGS=!ARGS! %%a )
)
if "%AUTO%"=="1" (
    echo ==^> checking module deps
    %PYEXE% module_deps.py || echo warn: module dep check failed; starting anyway >&2
)
rem A compiled module older than its .py (you edited the source) is dropped so
rem the edit runs; install.bat or venv\Scripts\python native_build.py rebuilds.
%PYEXE% native_build.py --check
rem CPython's JIT (3.13+ builds that ship it; a no-op elsewhere). set PYTHON_JIT=0 to turn it off.
if not defined PYTHON_JIT set PYTHON_JIT=1
echo ==^> starting; open http://localhost:8000 in a browser ^(Ctrl+C here stops it^)
%PYEXE% manager.py %ARGS%
if errorlevel 1 (
    echo.
    echo  the app stopped with an error ^(see above and logs\^)
    pause
)