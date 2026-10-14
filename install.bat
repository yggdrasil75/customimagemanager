@echo off
rem Native Windows install (the counterpart of install.sh). Modes:
rem
rem   install.bat                  detect GPU (cuda/cpu), full deps
rem   install.bat ultralight       viewer only: no torch, no ML
rem   install.bat cpu|cuda         force the backend (rocm: no Windows wheels, runs as cpu)
rem   install.bat cpu --minimal    backend wheels only; modules install their
rem                                own deps when you enable them in Settings
rem   install.bat --no-system      skip the system tools (winget / downloads)
rem   install.bat --no-compile     skip the native build of the Python modules
rem
rem Then: run.bat
rem
rem Tools the app runs (cjxl, ffmpeg, tailwindcss) go into tools\ and run.bat
rem puts that folder on PATH; nothing is installed system-wide unless winget
rem is present, in which case git / ffmpeg / 7zip / calibre go through it.
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set MODE=
set MINIMAL=0
set SYSTEM=1
set COMPILE=1
for %%a in (%*) do (
    if /i "%%a"=="ultralight" set MODE=ultralight
    if /i "%%a"=="cpu" set MODE=cpu
    if /i "%%a"=="cuda" set MODE=cuda
    if /i "%%a"=="rocm" set MODE=rocm
    if /i "%%a"=="--minimal" set MINIMAL=1
    if /i "%%a"=="--no-system" set SYSTEM=0
    if /i "%%a"=="--no-compile" set COMPILE=0
    if /i "%%a"=="-h" goto :help
    if /i "%%a"=="--help" goto :help
)

if "%MODE%"=="" (
    where nvidia-smi >nul 2>nul && nvidia-smi -L >nul 2>nul && set MODE=cuda
    if "!MODE!"=="" set MODE=cpu
    echo ==^> detected backend: !MODE!
)
if /i "%MODE%"=="rocm" (
    echo warn: torch has no ROCm wheels for Windows; installing the cpu backend >&2
    set MODE=cpu
)
if /i "%MODE%"=="ultralight" set MINIMAL=1

rem -- python ------------------------------------------------------------------
set PY=
where py >nul 2>nul && py -3 -c "import sys" >nul 2>nul && set PY=py -3
if "%PY%"=="" ( where python >nul 2>nul && set PY=python )
if "%PY%"=="" (
    echo error: Python 3 not found. Install it from https://www.python.org/downloads/ ^(tick "Add to PATH"^) >&2
    exit /b 1
)

rem -- system tools (best effort: a missing CLI degrades, it doesn't crash) ----
if "%SYSTEM%"=="1" (
    where winget >nul 2>nul
    if !errorlevel!==0 (
        set PKGS=Git.Git Gyan.FFmpeg 7zip.7zip
        if /i not "%MODE%"=="ultralight" set PKGS=!PKGS! calibre.calibre
        for %%p in (!PKGS!) do (
            echo ==^> winget: %%p
            winget install --id %%p -e --silent --accept-package-agreements --accept-source-agreements >nul 2>nul || echo warn: winget could not install %%p; continuing >&2
        )
    ) else (
        echo warn: winget not found - install git, ffmpeg, 7-zip ^(and calibre for books^) yourself and put them on PATH >&2
    )
    rem cjxl / djxl: the libjxl release build, into tools\ (run.bat adds tools\ to PATH)
    if not exist tools\cjxl.exe call :get_zip https://github.com/libjxl/libjxl/releases/download/v0.11.1/jxl-x64-windows-static.zip tools\jxl
    if exist tools\jxl\cjxl.exe ( move /y tools\jxl\*.exe tools\ >nul & rmdir /s /q tools\jxl )
    if not exist tools\cjxl.exe echo warn: cjxl not installed; JXL storage needs it ^(https://github.com/libjxl/libjxl/releases^) >&2
)

rem -- venv + python deps -------------------------------------------------------
if not exist venv\Scripts\python.exe %PY% -m venv venv || exit /b 1
set PIP=venv\Scripts\python.exe -m pip
%PIP% install -q --upgrade pip wheel

if /i "%MODE%"=="ultralight" (
    %PIP% install -r requirements-ultralight.txt || exit /b 1
) else if "%MINIMAL%"=="1" (
    %PIP% install -r requirements-%MODE%.txt -r requirements-ultralight.txt || exit /b 1
) else (
    %PIP% install -r requirements-%MODE%.txt -r requirements.txt || exit /b 1
)

rem -- vendored front-end JS + the Tailwind CLI (download to a temp name, then move) --
call :get https://cdn.tailwindcss.com/3.4.17 static\tailwindcss.js
call :get https://github.com/tailwindlabs/tailwindcss/releases/download/v3.4.17/tailwindcss-windows-x64.exe tools\tailwindcss.exe
call :get https://unpkg.com/three@0.137.5/build/three.min.js static\vendor\three.min.js
call :get https://unpkg.com/three@0.137.5/examples/js/loaders/OBJLoader.js static\vendor\OBJLoader.js
call :get https://unpkg.com/three@0.137.5/examples/js/controls/OrbitControls.js static\vendor\OrbitControls.js
call :get https://unpkg.com/drawflow@0.0.60/dist/drawflow.min.js static\vendor\drawflow.min.js
call :get https://unpkg.com/drawflow@0.0.60/dist/drawflow.min.css static\vendor\drawflow.min.css

rem -- native build of the Python modules (best effort; needs the MSVC Build Tools) --
if "%COMPILE%"=="1" (
    %PIP% install -q "cython<3.2" >nul 2>nul || echo warn: cython not installed; modules stay plain Python >&2
    venv\Scripts\python.exe native_build.py || echo warn: native build failed; modules stay plain Python >&2
)

if not exist media mkdir media
if not exist data mkdir data
if not exist logs mkdir logs
if not exist models mkdir models
venv\Scripts\python.exe -c "import json,os,sys; p='app_config.json'; c=json.load(open(p)) if os.path.exists(p) else {}; c['install']={'mode':sys.argv[1]}; json.dump(c,open(p,'w'),indent=2)" %MODE%
echo ==^> done. start it: run.bat
exit /b 0

rem -- helpers --------------------------------------------------------------------
:get
rem %1 url, %2 destination; skipped when the destination already has content
if exist "%~2" for %%s in ("%~2") do if %%~zs gtr 0 exit /b 0
if not exist "%~dp2" mkdir "%~dp2"
curl -fsSL "%~1" -o "%~2.tmp" && for %%s in ("%~2.tmp") do if %%~zs gtr 0 ( move /y "%~2.tmp" "%~2" >nul & exit /b 0 )
del /q "%~2.tmp" 2>nul
echo warn: download failed: %~1 >&2
exit /b 0

:get_zip
rem %1 url, %2 folder to extract into (tar on Windows 10+ reads zip files)
if not exist "%~2" mkdir "%~2"
curl -fsSL "%~1" -o "%~2\dl.zip" && tar -xf "%~2\dl.zip" -C "%~2" && del /q "%~2\dl.zip" && exit /b 0
echo warn: download failed: %~1 >&2
exit /b 0

:help
findstr /b /c:"rem " "%~f0"
exit /b 0