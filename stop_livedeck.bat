@echo off
rem 停止多屏直播监控台：只杀监听 8760 的进程 + 该服务的 Edge 桥，不动你自己的浏览器
cd /d "%~dp0"
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8760" ^| findstr "LISTENING"') do taskkill /F /PID %%p >nul 2>&1
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'msedge.exe' -and $_.CommandLine -like '*livedeck-edge-*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1
echo stopped.
