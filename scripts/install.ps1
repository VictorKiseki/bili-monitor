#Requires -Version 5.1
<#
.SYNOPSIS
    一键安装：把「刚解压的项目」变成「监控台已经打开、账号待绑」的状态。

.DESCRIPTION
    这个版本不再联网装依赖 —— 项目自带 Python 运行时（runtime\），依赖已经预装在里面。
    安装五步：
      1) 校验 runtime\python.exe 能跑、五个必需依赖都能 import
      2) 从 *.example 生成配置骨架（已存在的文件绝不覆盖）
      3) 建 data/ logs/ out/ dist/，收紧凭据权限
      4) 跑环境自检（凭据没填只算警告，不算失败）
      5) 启动监控台（先只起页面），自动打开浏览器，并顺手拉起监控室

    安装过程不需要任何绑定信息：B 站账号、QQ 机器人都在监控台页面里绑，
    绑上之后采集进程 / 推送进程会自动开启。

    想重建运行时（或升级 CPython）：python scripts\build_runtime.py --force

.EXAMPLE
    .\install.cmd
    .\scripts\install.ps1 -SkipAcl
    .\scripts\install.ps1 -SkipStart -NoOpen      # 只准备环境，不启动、不开浏览器
#>
[CmdletBinding()]
param(
    # 前四个是为老版本（系统 Python + .venv）保留的兼容参数，现在不再需要，传了只提示
    [string]$Python = '',
    [switch]$NoVenv,
    [string]$Mirror = '',
    [switch]$Force,
    [switch]$SkipAcl,
    [switch]$SkipCheck,
    [switch]$SkipStart,
    [switch]$NoOpen,
    [int]$Port = 8765
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
try { $OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$Root = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path (Join-Path $Root 'VERSION'))) {
    Write-Host "[失败] 找不到项目根目录（应在 <项目>\scripts\install.ps1 处运行）" -ForegroundColor Red
    exit 1
}
Set-Location $Root
$Version = (Get-Content -Path (Join-Path $Root 'VERSION') -TotalCount 1).Trim()

$script:TotalSteps = 5
function Step([int]$n, [string]$msg) { Write-Host ''; Write-Host ("[{0}/{1}] {2}" -f $n, $script:TotalSteps, $msg) -ForegroundColor Cyan }
function Ok([string]$msg) { Write-Host ("  [OK]   {0}" -f $msg) -ForegroundColor Green }
function Warn([string]$msg) { Write-Host ("  [WARN] {0}" -f $msg) -ForegroundColor Yellow }
function Info([string]$msg) { Write-Host ("         {0}" -f $msg) -ForegroundColor DarkGray }
function Fail([string]$msg) { Write-Host ("  [FAIL] {0}" -f $msg) -ForegroundColor Red }

function Test-PortOpen([int]$p) {
    # 只用 .NET 基础类：不依赖 Test-NetConnection（老系统上没有），也不弹任何交互
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $iar = $client.BeginConnect('127.0.0.1', $p, $null, $null)
        if (-not $iar.AsyncWaitHandle.WaitOne(400)) { $client.Close(); return $false }
        $client.EndConnect($iar)
        $client.Close()
        return $true
    } catch {
        return $false
    }
}

function Wait-PortOpen([int]$p, [int]$seconds) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-PortOpen $p) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return (Test-PortOpen $p)
}

Write-Host ''
Write-Host ("B 站监控 + QQ 推送  一键安装 v{0}" -f $Version) -ForegroundColor White
Write-Host ("项目目录：{0}" -f $Root) -ForegroundColor DarkGray
Write-Host ("系统：{0} / PowerShell {1}" -f [System.Environment]::OSVersion.VersionString, $PSVersionTable.PSVersion) -ForegroundColor DarkGray

if ($Python -or $NoVenv -or $Mirror -or $Force) {
    Warn '现在是自带运行时（runtime\），-Python / -NoVenv / -Mirror / -Force 已不再需要，已忽略'
}

# ---------------------------------------------------------------- 1) 校验自带运行时
Step 1 '校验自带的 Python 运行时（runtime\）'

$script:PyExe = ''
$rtPy = Join-Path $Root 'runtime\python.exe'
$venvPy = Join-Path $Root '.venv\Scripts\python.exe'

if (Test-Path -LiteralPath $rtPy) {
    $script:PyExe = $rtPy
    Ok ("自带运行时：{0}" -f $rtPy)
} elseif (Test-Path -LiteralPath $venvPy) {
    $script:PyExe = $venvPy
    Warn '没有 runtime\，退回开发机的 .venv（发布包不应该出现这种情况）'
} else {
    Fail '既没有 runtime\python.exe，也没有 .venv\Scripts\python.exe'
    Write-Host '       这个包不完整：请重新解压发布 zip；' -ForegroundColor Red
    Write-Host '       或者在有网络的开发机上跑 python scripts\build_runtime.py 重建运行时。' -ForegroundColor Red
    exit 1
}

# 探针里不能出现任何引号：Windows PowerShell 5.1 会把内置引号从原生参数里剥掉。
# 只靠退出码判断：依赖缺失时 python 自己返回 1。
$script:Probe = 'import sys;print(*sys.version_info[:3]);import httpx,PIL,websockets,websocket,segno'
$probeOut = @()
$probeOk = $false
try {
    $probeOut = & $script:PyExe -c $script:Probe 2>&1
    $probeOk = ($LASTEXITCODE -eq 0)
} catch { $probeOk = $false }
if (-not $probeOk) {
    Fail '运行时跑不起来或依赖不全'
    Info (($probeOut | Out-String).Trim())
    Write-Host '       在有网络的开发机上重建：python scripts\build_runtime.py --force' -ForegroundColor Red
    exit 1
}
$verParts = @(([string](@($probeOut)[0])).Trim() -split '\s+')
$verText = if ($verParts.Count -ge 3) { $verParts[0..2] -join '.' } else { ([string](@($probeOut)[0])).Trim() }
Ok ("解释器可用：Python {0}" -f $verText)
Ok '五个必需依赖齐全（httpx / Pillow / websockets / websocket-client / segno）'

# ---------------------------------------------------------------- 2) 生成配置
Step 2 '生成配置骨架（已存在的文件不会被覆盖）'

$pairs = @(
    @('BBOT.env.example', 'BBOT.env'),
    @('config\bili.env.example', 'config\bili.env'),
    @('config\subjects.json.example', 'config\subjects.json'),
    @('config\recorder_cleanup.json.example', 'config\recorder_cleanup.json'),
    @('config\livedeck.env.example', 'config\livedeck.env')
)
$created = @()
foreach ($p in $pairs) {
    $src = Join-Path $Root $p[0]
    $dst = Join-Path $Root $p[1]
    if (-not (Test-Path $src)) { continue }
    if (Test-Path $dst) {
        Info ("已存在，保持不动：{0}" -f $p[1])
        continue
    }
    $dstDir = Split-Path -Parent $dst
    if (-not (Test-Path $dstDir)) { New-Item -ItemType Directory -Path $dstDir -Force | Out-Null }
    Copy-Item -Path $src -Destination $dst
    $created += $p[1]
    Ok ("已生成：{0}" -f $p[1])
}
if ($created.Count -eq 0) { Info '配置都在了，没有新生成' }
Info '这些都是空壳/模板：真实账号在监控台页面里绑，不需要改文件'

# ---------------------------------------------------------------- 3) 目录 + 权限
Step 3 '创建运行目录并收紧凭据权限'

foreach ($d in @('data', 'logs', 'out', 'dist')) {
    $path = Join-Path $Root $d
    if (-not (Test-Path $path)) { New-Item -ItemType Directory -Path $path -Force | Out-Null }
}
Ok 'data/ logs/ out/ dist/ 已就绪'

if ($SkipAcl) {
    Warn '按 -SkipAcl 跳过权限收紧（凭据文件可能对其他用户可读）'
} elseif (Test-Path (Join-Path $Root 'scripts\secure_config.py')) {
    & $script:PyExe (Join-Path $Root 'scripts\secure_config.py') | Out-Null
    if ($LASTEXITCODE -eq 0) { Ok '凭据文件权限已收紧（仅当前用户可读）' } else { Warn '权限收紧没完全成功，可以稍后手动跑 scripts\secure_config.py' }
} else { Info '没有 secure_config.py，跳过' }

# ---------------------------------------------------------------- 4) 自检
Step 4 '环境自检（账号没绑只算警告）'

$preflight = Join-Path $Root 'scripts\preflight.py'
$checkOk = $true
if ($SkipCheck) {
    Warn '按 -SkipCheck 跳过自检'
} elseif (Test-Path $preflight) {
    & $script:PyExe $preflight
    if ($LASTEXITCODE -ne 0) { $checkOk = $false }
} else { Warn '没有 preflight.py，跳过' }

# ---------------------------------------------------------------- 5) 启动监控台
Step 5 '启动监控台（先起页面，账号在页面里绑）'

$url = 'http://127.0.0.1:' + $Port
$started = $false
$consolePy = Join-Path $Root 'scripts\run_console.py'
$pythonw = Join-Path $Root 'runtime\pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonw)) { $pythonw = Join-Path $Root '.venv\Scripts\pythonw.exe' }
if (-not (Test-Path -LiteralPath $pythonw)) { $pythonw = $script:PyExe }

if ($SkipStart) {
    Warn '按 -SkipStart 跳过启动（稍后手动运行 scripts\service.ps1 start）'
} elseif (Test-PortOpen $Port) {
    Ok ('监控台已经在跑：' + $url)
    $started = $true
} else {
    $svc = Join-Path $Root 'scripts\service.ps1'
    if (Test-Path -LiteralPath $svc) {
        try {
            & $svc -Action start -Port $Port -Python $script:PyExe | Out-Null
        } catch {
            Warn ('service.ps1 启动失败：' + $_.Exception.Message.Trim())
        }
    }
    if (Wait-PortOpen $Port 25) {
        $started = $true
        Ok ('监控台已启动（守护托管）：' + $url)
    } else {
        # 兜底：不经过守护，直接起监控台主进程；页面能用，只是没有崩溃重启
        try {
            Start-Process -FilePath $pythonw -ArgumentList @('scripts\run_console.py', '--port', "$Port") -WorkingDirectory $Root -WindowStyle Hidden
            if (Wait-PortOpen $Port 20) {
                $started = $true
                Ok ('监控台已启动（直接运行，无守护）：' + $url)
            }
        } catch {
            Warn ('直接启动 run_console.py 失败：' + $_.Exception.Message.Trim())
        }
    }
    if (-not $started) {
        Warn ('监控台还没起来：稍后运行 scripts\service.ps1 start，或再双击 install.cmd 重试')
        Info ('也可以手动看日志：logs\console.log / logs\bot.console.log')
    }
}

if ($started -and -not $NoOpen) {
    try {
        Start-Process $url
        Ok '已在默认浏览器打开监控台'
    } catch {
        Warn ('自动打开浏览器失败，请手动打开 ' + $url)
    }
}

# 监控室（弹幕多屏台 8760）尽力而为：起不来不影响主流程
$ldPort = 8760
$ldPy = Join-Path $Root 'run_livedeck.py'
if (Test-Path -LiteralPath $ldPy) {
    if (Test-PortOpen $ldPort) {
        Ok ('监控室已经在跑：http://127.0.0.1:' + $ldPort)
    } else {
        try {
            Start-Process -FilePath $pythonw -ArgumentList @('run_livedeck.py', '--port', "$ldPort", '--no-open') -WorkingDirectory $Root -WindowStyle Hidden
            if (Wait-PortOpen $ldPort 12) {
                Ok ('监控室已启动：http://127.0.0.1:' + $ldPort)
            } else {
                Warn '监控室没起来（不影响监控台与推送）；稍后可双击 start_livedeck.bat'
            }
        } catch {
            Warn ('监控室没起来：' + $_.Exception.Message.Trim())
        }
    }
}

# ---------------------------------------------------------------- 收尾
Write-Host ''
Write-Host '============================================================' -ForegroundColor DarkGray
if ($checkOk) {
    Write-Host ' 安装完成。' -ForegroundColor Green
} else {
    Write-Host ' 运行时与页面已就绪，但自检有阻塞项（见上面 [FAIL] 行）。' -ForegroundColor Yellow
}
Write-Host ''
Write-Host ' 接下来全在页面里做（不用改任何文件）：' -ForegroundColor White
Write-Host ('   1. 打开监控台：' + $url)
Write-Host '   2. 右上角「接入指引」→ 绑 B 站账号（扫码）与 QQ 机器人（AppID / AppSecret + 抓推送目标）'
Write-Host '   3. 绑上之后：采集进程 / 推送进程会自动开启（「进程」页能看到）'
Write-Host ''
Write-Host (' 监控室（弹幕多屏台）：http://127.0.0.1:' + $ldPort)
Write-Host ' 命令行也能用：scripts\service.ps1 status | start | stop；详细说明：docs\31-发布与交接手册.md' -ForegroundColor DarkGray
Write-Host '============================================================' -ForegroundColor DarkGray

if ($checkOk) { exit 0 } else { exit 1 }
