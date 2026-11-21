@echo off
chcp 65001 >nul
rem self-locating: %~dp0 is the scripts\ folder, its parent is the project root
set "ROOT=%~dp0.."
pushd "%ROOT%" || exit /b 1
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
rem prefer bundled runtime, then dev .venv, then PATH
set "PY=%CD%\runtime\python.exe"
if not exist "%PY%" set "PY=%CD%\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
rem main process = console only; collector/pusher are started by it once accounts are bound
"%PY%" scripts\supervisor.py --log-dir logs --db data\bot.db --state-file data\supervisor.json --stop-file data\supervisor.stop --child-stop-file data\console.stop --worker-stop-file data\schedule_worker.stop --restart-file data\restart.request -- "%PY%" scripts\run_console.py --port 8765
set "RC=%ERRORLEVEL%"
popd
exit /b %RC%
