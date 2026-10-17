@echo off
rem git pull, then re-install the python deps for the mode you installed with
rem (the counterpart of update.sh). Never touches system tools: that's install.bat's first-run job.
setlocal
cd /d "%~dp0"
set PY=venv\Scripts\python.exe
if not exist %PY% set PY=python\python.exe
if not exist %PY% set PY=python
if not exist .git (
    echo This is a portable release ^(no git checkout^): get the new zip from
    echo   https://github.com/yggdrasil75/customimagemanager/releases
    echo and unpack it over this folder; media\, data\, models\ and app_config.json are kept.
    pause
    exit /b 1
)
git pull --ff-only || ( pause & exit /b 1 )
rem A compiled module older than its pulled source is dropped; install.bat
rem rebuilds those alone (the rest stay compiled: no full rebuild per update).
%PY% native_build.py --check
set MODE=cpu
for /f "usebackq delims=" %%m in (`%PY% -c "import json;print(json.load(open('app_config.json')).get('install',{}).get('mode','cpu'))" 2^>nul`) do set MODE=%%m
call install.bat %MODE% --no-system
echo ==^> restart: run.bat