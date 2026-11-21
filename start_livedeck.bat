@echo off
rem silent launcher for the multi-screen live deck (called by start_livedeck.vbs)
cd /d "%~dp0"
if not exist logs mkdir logs
rem prefer bundled runtime (unzip and run), then dev .venv, then PATH
set "PY=%~dp0runtime\python.exe"
if not exist "%PY%" set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" -u run_livedeck.py --no-open >> logs\livedeck.log 2>&1
