@echo off
rem Whole suite, headless (the counterpart of run_tests.sh): core tests (tests\),
rem module tests (modules\*\tests\), generic provider tests, and the jsdom
rem frontend (via tests\test_frontend.py).
rem Needs: pytest, pyexiv2, imagecodecs, cjxl; node+npm for the JS half.
rem   run_tests.bat                     everything
rem   run_tests.bat modules\people      one module
rem   run_tests.bat tests\test_providers.py -k pose
rem   run_tests.bat --cim-fixtures D:\fixtures --cim-config app_config.json --cim-remote
rem   run_tests.bat --cim-all-variants pose        every pose model, every size/type
rem   run_tests.bat --cim-no-models                skip everything that loads a model
rem Options: run_tests.bat --help (section "CIM test kit").
setlocal EnableDelayedExpansion
cd /d "%~dp0"
set "PATH=%~dp0tools;%PATH%"
set PY=venv\Scripts\python.exe
if not exist %PY% set PY=python
rem pytest reads a space-separated value of a plugin option as a test path before
rem the plugin loads; pass --cim-x=VALUE so it can't.
set ARGS=
:next
if "%~1"=="" goto run
set JOIN=0
for %%o in (--cim-config --cim-fixtures --cim-all-variants --cim-timeout) do if /i "%~1"=="%%o" set JOIN=1
if "%JOIN%"=="1" (
    set ARGS=!ARGS! %~1=%~2
    shift
) else (
    set ARGS=!ARGS! %1
)
shift
goto next
:run
%PY% -m pytest -c pytest.ini --rootdir . %ARGS%