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
rem   install.bat --no-pause       do not wait for a key at the end (scripts, update.bat)
rem
rem Then: run.bat
rem
rem Every step says what it does and the window stays open at the end (and on
rem an error) so you can read it. Nothing pops up: winget and pip run quietly
rem in this window. A second run only does what is missing: python deps are
rem installed again only when a requirements file changed, downloads are
rem skipped when present, compiled modules are rebuilt only when their source
rem changed.
rem
rem Tools the app runs (cjxl, ffmpeg, tailwindcss) go into tools\ and run.bat
rem puts that folder on PATH; nothing is installed system-wide unless winget
rem is present, in which case Python (when missing) / git / ffmpeg / 7zip go
rem through it, per user.
setlocal EnableDelayedExpansion
cd /d "%~dp0"
set STEP=0

set MODE=
set MINIMAL=0
set SYSTEM=1
set COMPILE=1
set PAUSE=1
for %%a in (%*) do (
    if /i "%%a"=="ultralight" set MODE=ultralight
    if /i "%%a"=="cpu" set MODE=cpu
    if /i "%%a"=="cuda" set MODE=cuda
    if /i "%%a"=="rocm" set MODE=rocm
    if /i "%%a"=="--minimal" set MINIMAL=1
    if /i "%%a"=="--no-system" set SYSTEM=0
    if /i "%%a"=="--no-compile" set COMPILE=0
    if /i "%%a"=="--no-pause" set PAUSE=0
    if /i "%%a"=="-h" goto :help
    if /i "%%a"=="--help" goto :help
)

echo.
echo  Custom Image Manager - install
echo  ==============================
if "%MODE%"=="" (
    where nvidia-smi >nul 2>nul && nvidia-smi -L >nul 2>nul && set MODE=cuda
    if "!MODE!"=="" set MODE=cpu
    echo  detected backend: !MODE!
)
if /i "%MODE%"=="rocm" (
    echo  note: torch has no ROCm wheels for Windows; installing the cpu backend
    set MODE=cpu
)
if /i "%MODE%"=="ultralight" set MINIMAL=1
echo  mode: %MODE%   (minimal=%MINIMAL%, system tools=%SYSTEM%, native build=%COMPILE%)
echo.

rem -- python ------------------------------------------------------------------
call :step "Python"
call :find_python
if "%PY%"=="" (
    where winget >nul 2>nul
    if !errorlevel!==0 (
        echo      Python 3 is not installed; installing it for this user with winget...
        winget install --id Python.Python.3.12 -e --silent --scope user --disable-interactivity --accept-package-agreements --accept-source-agreements
        call :find_python
    )
)
if "%PY%"=="" (
    echo      Python 3 is not installed; downloading the python.org installer...
    call :get https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe tools\python-installer.exe
    if exist tools\python-installer.exe (
        echo      installing Python 3.12 for this user ^(a minute^)...
        tools\python-installer.exe /quiet InstallAllUsers=0 PrependPath=1 Include_test=0 Include_launcher=1
        call :find_python
    )
)
if "%PY%"=="" call :fail "Python 3 could not be found or installed. Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH') and run install.bat again."
echo      using: %PY%

rem -- system tools (best effort: a missing CLI degrades, it doesn't crash) ----
if "%SYSTEM%"=="1" (
    call :step "command-line tools (git, ffmpeg, 7-zip; cjxl into tools\)"
    where winget >nul 2>nul
    if !errorlevel!==0 (
        for %%p in (Git.Git Gyan.FFmpeg 7zip.7zip) do (
            call :have_winget %%p || (
                echo      winget: %%p
                winget install --id %%p -e --silent --scope user --disable-interactivity --accept-package-agreements --accept-source-agreements >nul 2>nul || echo      warn: winget could not install %%p; continuing
            )
        )
    ) else (
        echo      warn: winget not found - install git, ffmpeg and 7-zip yourself and put them on PATH
    )
    rem cjxl / djxl / jxlinfo: the libjxl release build, into tools\ (run.bat adds tools\ to PATH)
    if not exist tools\cjxl.exe call :get_zip https://github.com/libjxl/libjxl/releases/download/v0.11.1/jxl-x64-windows-static.zip tools\jxl
    if exist tools\jxl\cjxl.exe ( move /y tools\jxl\*.exe tools\ >nul & rmdir /s /q tools\jxl )
    if not exist tools\cjxl.exe echo      warn: cjxl not installed; JXL storage needs it ^(https://github.com/libjxl/libjxl/releases^)
)

rem -- venv + python deps -------------------------------------------------------
call :step "Python packages (the first time this is the long part)"
if not exist venv\Scripts\python.exe %PY% -m venv venv || call :fail "could not create the venv"
set PIP=venv\Scripts\python.exe -m pip
venv\Scripts\python.exe -c "import wheel" >nul 2>nul || %PIP% install -q --upgrade pip wheel

rem the requirement files that apply; pip runs only when they changed since the
rem last successful install (a stamp of their content in the venv)
if /i "%MODE%"=="ultralight" ( set REQS=requirements-ultralight.txt
) else if "%MINIMAL%"=="1" ( set REQS=requirements-%MODE%.txt requirements-ultralight.txt
) else ( set REQS=requirements-%MODE%.txt requirements.txt )
set STAMP=venv\.cim-deps-%MODE%
set WANT=
for /f "usebackq delims=" %%h in (`venv\Scripts\python.exe install_check.py stamp %REQS%`) do set WANT=%%h
set HAVE=
if exist "%STAMP%" set /p HAVE=<"%STAMP%"
if "%HAVE%"=="%WANT%" (
    echo      unchanged ^(%REQS%^); nothing to install
) else (
    set RARGS=
    for %%r in (%REQS%) do set RARGS=!RARGS! -r %%r
    %PIP% install !RARGS! || call :fail "pip could not install the Python packages (see above)"
    >"%STAMP%" echo %WANT%
)
rem onnxruntime / onnxruntime-gpu install the same files; requirements.txt pulls the
rem CPU one in as a dependency on top of the GPU build. Keep the one the backend
rem wants and put its files back - only when it is not already the only one that
rem imports with its provider.
if /i not "%MODE%"=="ultralight" (
    if /i "%MODE%"=="cuda" ( set ORT=onnxruntime-gpu& set ORTP=CUDAExecutionProvider ) else ( set ORT=onnxruntime& set ORTP= )
    venv\Scripts\python.exe install_check.py ort !ORT! !ORTP! >nul 2>nul
    if !errorlevel! neq 0 (
        echo      onnxruntime: putting !ORT! in place
        for %%o in (onnxruntime onnxruntime-gpu onnxruntime-migraphx onnxruntime-rocm) do if /i not "%%o"=="!ORT!" %PIP% uninstall -qy %%o >nul 2>nul
        %PIP% install -q --force-reinstall --no-deps !ORT! || %PIP% install -q --force-reinstall --no-deps onnxruntime
    ) else (
        echo      onnxruntime: !ORT! in place
    )
)

rem -- vendored front-end JS + the Tailwind CLI (download to a temp name, then move) --
call :step "front-end files (skipped when present)"
call :get https://cdn.tailwindcss.com/3.4.17 static\tailwindcss.js
call :get https://github.com/tailwindlabs/tailwindcss/releases/download/v3.4.17/tailwindcss-windows-x64.exe tools\tailwindcss.exe
call :get https://unpkg.com/three@0.137.5/build/three.min.js static\vendor\three.min.js
call :get https://unpkg.com/three@0.137.5/examples/js/loaders/OBJLoader.js static\vendor\OBJLoader.js
call :get https://unpkg.com/three@0.137.5/examples/js/controls/OrbitControls.js static\vendor\OrbitControls.js
call :get https://unpkg.com/drawflow@0.0.60/dist/drawflow.min.js static\vendor\drawflow.min.js
call :get https://unpkg.com/drawflow@0.0.60/dist/drawflow.min.css static\vendor\drawflow.min.css

rem -- native build of the Python modules (optional; needs the MSVC Build Tools) --
if "%COMPILE%"=="1" (
    call :step "compiled modules (optional; only with a C compiler, only what changed)"
    where cl >nul 2>nul
    if !errorlevel!==0 (
        venv\Scripts\python.exe -c "import Cython" >nul 2>nul || %PIP% install -q "cython<3.2" >nul 2>nul
        venv\Scripts\python.exe native_build.py || echo      warn: native build failed; modules stay plain Python
    ) else (
        echo      no C compiler ^(cl.exe^): modules run as plain Python, which is fine.
        echo      For the compiled build install "Build Tools for Visual Studio" and run install.bat from its developer prompt.
    )
)

call :step "folders and config"
if not exist media mkdir media
if not exist data mkdir data
if not exist logs mkdir logs
if not exist models mkdir models
venv\Scripts\python.exe -c "import json,os,sys; p='app_config.json'; c=json.load(open(p)) if os.path.exists(p) else {}; c['install']={'mode':sys.argv[1]}; json.dump(c,open(p,'w'),indent=2)" %MODE%
echo.
echo  ==============================
echo  DONE. Start the app with run.bat, then open http://localhost:8000
echo  ==============================
if "%PAUSE%"=="1" pause
exit /b 0

rem -- helpers --------------------------------------------------------------------
:step
set /a STEP+=1
echo.
echo ==^> %STEP%. %~1
exit /b 0

:fail
echo.
echo  ERROR: %~1
echo.
if "%PAUSE%"=="1" pause
exit 1

:find_python
rem sets PY to a working Python 3 launcher, or leaves it empty
set PY=
where py >nul 2>nul && py -3 -c "import sys" >nul 2>nul && set PY=py -3
if "%PY%"=="" ( where python >nul 2>nul && python -c "import sys; sys.exit(0 if sys.version_info[0]==3 else 1)" >nul 2>nul && set PY=python )
rem a fresh install is not on this window's PATH yet: look where the installers put it
if "%PY%"=="" for /d %%d in ("%LocalAppData%\Programs\Python\Python3*") do if exist "%%d\python.exe" set "PY=%%d\python.exe"
exit /b 0

:have_winget
rem exit 0 when the winget package id is installed
winget list --id %~1 -e --disable-interactivity --accept-source-agreements 2>nul | findstr /i /c:"%~1" >nul 2>nul
exit /b %errorlevel%

:get
rem %1 url, %2 destination; skipped when the destination already has content
if exist "%~2" for %%s in ("%~2") do if %%~zs gtr 0 exit /b 0
if not exist "%~dp2" mkdir "%~dp2"
echo      downloading %~nx2
curl -fsSL "%~1" -o "%~2.tmp" && for %%s in ("%~2.tmp") do if %%~zs gtr 0 ( move /y "%~2.tmp" "%~2" >nul & exit /b 0 )
del /q "%~2.tmp" 2>nul
echo      warn: download failed: %~1
exit /b 0

:get_zip
rem %1 url, %2 folder to extract into (tar on Windows 10+ reads zip files)
if not exist "%~2" mkdir "%~2"
echo      downloading %~nx1
curl -fsSL "%~1" -o "%~2\dl.zip" && tar -xf "%~2\dl.zip" -C "%~2" && del /q "%~2\dl.zip" && exit /b 0
echo      warn: download failed: %~1
exit /b 0

:help
findstr /b /c:"rem " "%~f0"
exit /b 0