@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo.
echo   B 站监控 + QQ 推送  一键安装（自带 Python 运行时，不联网）
echo   装完会自动打开监控台；账号（B 站 / QQ 机器人）都在页面里绑，不用改文件
echo.

set "PS=powershell"
where pwsh >nul 2>nul && set "PS=pwsh"

%PS% -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install.ps1" %*
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
    echo [完成] 安装成功。监控台地址： http://127.0.0.1:8765
    echo 如果浏览器没有自动打开，把上面这个地址复制到浏览器即可。
    echo 账号绑定：页面右上角「接入指引」；绑定后采集 / 推送会自动开启。
) else (
    echo [失败] 安装没有完成，退出码 %RC%
    echo 上面的报错信息就是原因；修好后再双击 install.cmd 重跑一次即可。
)
echo.
echo 按任意键关闭这个窗口（关闭不影响已经启动的监控台）...
pause >nul
exit /b %RC%
