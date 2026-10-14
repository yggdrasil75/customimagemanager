@echo off
rem git pull, then re-install the python deps for the mode you installed with
rem (the counterpart of update.sh). Never touches system tools: that's install.bat's first-run job.
setlocal
cd /d "%~dp0"
set PY=venv\Scripts\python.exe
if not exist %PY% set PY=python
rem Compiled modules shadow their .py: drop them BEFORE pulling so the pulled
rem source is what runs (install.bat rebuilds them afterwards).
%PY% native_build.py --clean
git pull --ff-only || exit /b 1
set MODE=cpu
for /f "usebackq delims=" %%m in (`%PY% -c "import json;print(json.load(open('app_config.json')).get('install',{}).get('mode','cpu'))" 2^>nul`) do set MODE=%%m
call install.bat %MODE% --no-system
echo ==^> restart: run.bat