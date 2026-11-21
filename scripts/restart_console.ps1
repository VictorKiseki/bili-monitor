<#
  restart_console.ps1 — 只重启「Web 管理台」这个主进程（默认 127.0.0.1:8765）

  用法（在仓库根目录，或任意目录用绝对路径）：
    pwsh -File scripts\restart_console.ps1               # 重启管理台
    pwsh -File scripts\restart_console.ps1 -Status       # 只看现状，不动进程
    pwsh -File scripts\restart_console.ps1 -Force        # 不走优雅退出，直接结束监听进程
    pwsh -File scripts\restart_console.ps1 -Start        # 没在跑时，单独起一个管理台
    pwsh -File scripts\restart_console.ps1 -Port 9000 -TimeoutSec 180

  两条路径：
    1) 进程在 data\supervisor.json 那个守护下面 → 写 data\restart.request，
       守护让主进程优雅退出后立即重启（不计崩溃次数，守护自己不动）。
       和管理台「服务」页「重启采集进程」按钮走同一条路（docs/12 §11、docs/13 §9.2）。
    2) 不在守护下面（例如直接 python scripts\run_console.py）→ 记下原命令行，
       结束进程后用 pythonw 重新拉起，输出进 logs\console.log，且不在当前终端的作业对象里。

  注意：
    - 只重启管理台主进程。多屏直播监控台（8760）不归它管，传错端口会直接报错。
    - 日程功能开着时，守护会连带重启日程进程组（schedule_worker.py），这是守护的既定行为。
    - 退出码：0 成功；1 失败（端口被占 / 守护没重启 / 超时）。

  历史：2026-11-09 新增。之前只能用 scripts\service.ps1 restart（整条守护链重来）。
#>
[CmdletBinding()]
param(
  [int]$Port = 8765,
  [int]$TimeoutSec = 120,
  [int]$GraceSec = 20,
  [switch]$Force,
  [switch]$Start,
  [switch]$Status
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$DataDir = Join-Path $Root 'data'
$LogDir = Join-Path $Root 'logs'
$StateFile = Join-Path $DataDir 'supervisor.json'
$RestartFile = Join-Path $DataDir 'restart.request'
$ConsoleLog = Join-Path $LogDir 'console.log'

function Write-Step([string]$Text) { Write-Host ('[重启] ' + $Text) -ForegroundColor Cyan }
function Write-Ok([string]$Text) { Write-Host ('[成功] ' + $Text) -ForegroundColor Green }
function Write-Warn2([string]$Text) { Write-Host ('[注意] ' + $Text) -ForegroundColor Yellow }

function Get-PythonPath {
  $rtPython = Join-Path $Root 'runtime\python.exe'
  if (Test-Path -LiteralPath $rtPython) { return $rtPython }
  $venvPython = Join-Path $Root '.venv\Scripts\python.exe'
  if (Test-Path -LiteralPath $venvPython) { return $venvPython }
  foreach ($name in @('python', 'py')) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
  }
  throw '找不到 Python 解释器：请先运行 install.cmd（自带运行时在 runtime\）'
}

# 监听指定端口的进程 pid；没有就返回 0
function Get-ListenPid([int]$ListenPort) {
  if (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue) {
    $conn = Get-NetTCPConnection -LocalPort $ListenPort -State Listen -ErrorAction SilentlyContinue |
      Where-Object { $_.OwningProcess -gt 0 } | Select-Object -First 1
    if ($conn) { return [int]$conn.OwningProcess }
    return 0
  }
  # 精简环境没有 NetTCPIP 模块时退回 netstat 解析
  foreach ($row in (netstat -ano -p tcp)) {
    $cols = @(($row -split '\s+') | Where-Object { $_ })
    if ($cols.Count -lt 5) { continue }
    if ($cols[3] -ne 'LISTENING') { continue }
    if ($cols[1] -notmatch (':' + $ListenPort + '$')) { continue }
    return [int]$cols[4]
  }
  return 0
}

function Get-ProcessInfo([int]$ProcId) {
  if ($ProcId -le 0) { return $null }
  try {
    return Get-CimInstance Win32_Process -Filter ('ProcessId=' + $ProcId) -ErrorAction Stop
  } catch {
    return $null
  }
}

function Test-Alive([int]$ProcId) {
  if ($ProcId -le 0) { return $false }
  return [bool](Get-Process -Id $ProcId -ErrorAction SilentlyContinue)
}

# 从 pid 往上找父进程，最多 10 层
function Get-Chain([int]$ProcId) {
  $list = New-Object System.Collections.ArrayList
  $seen = @{}
  $cur = $ProcId
  for ($i = 0; $i -lt 10; $i++) {
    if ($cur -le 0 -or $seen.ContainsKey($cur)) { break }
    $seen[$cur] = $true
    $info = Get-ProcessInfo $cur
    if (-not $info) { break }
    [void]$list.Add($info)
    if ([int]$info.ParentProcessId -le 0) { break }
    $cur = [int]$info.ParentProcessId
  }
  return $list
}

function Read-SupervisorState {
  if (-not (Test-Path -LiteralPath $StateFile)) { return $null }
  try {
    return (Get-Content -LiteralPath $StateFile -Raw -Encoding UTF8 | ConvertFrom-Json)
  } catch {
    return $null
  }
}

function Get-SupervisorInfo {
  $state = Read-SupervisorState
  $supPid = 0
  $childPid = 0
  if ($state) {
    if ($state.pid) { $supPid = [int]$state.pid }
    if ($state.child_pid) { $childPid = [int]$state.child_pid }
  }
  return [pscustomobject]@{ state = $state; pid = $supPid; child_pid = $childPid; alive = (Test-Alive $supPid) }
}

# supervised：在守护进程树里；standalone：独立进程；stopped：没在跑
function Get-ConsoleMode([int]$ListenPid) {
  if ($ListenPid -le 0) { return 'stopped' }
  $sup = Get-SupervisorInfo
  $chain = @(Get-Chain $ListenPid)
  if ($sup.alive) {
    foreach ($p in $chain) {
      $procId = [int]$p.ProcessId
      if ($procId -eq $sup.pid -or ($sup.child_pid -gt 0 -and $procId -eq $sup.child_pid)) { return 'supervised' }
    }
  }
  foreach ($p in $chain) {
    if ($p.CommandLine -and ([string]$p.CommandLine) -match 'supervisor\.py') { return 'supervised' }
  }
  return 'standalone'
}

function Test-PortOpen([int]$ListenPort, [int]$WaitMs = 800) {
  $client = New-Object System.Net.Sockets.TcpClient
  try {
    $async = $client.BeginConnect('127.0.0.1', $ListenPort, $null, $null)
    if (-not $async.AsyncWaitHandle.WaitOne($WaitMs)) { return $false }
    $client.EndConnect($async)
    return $true
  } catch {
    return $false
  } finally {
    $client.Close()
  }
}

function Get-HttpStatus([int]$ListenPort) {
  try {
    $resp = Invoke-WebRequest -Uri ('http://127.0.0.1:' + $ListenPort + '/') -UseBasicParsing -TimeoutSec 5
    return [int]$resp.StatusCode
  } catch {
    return 0
  }
}

# 等到「端口换了新监听者且 HTTP 200」；返回新 pid，超时返回 0
function Wait-Console([int]$ListenPort, [int]$OldPid, [datetime]$Deadline) {
  while ((Get-Date) -lt $Deadline) {
    $nowPid = Get-ListenPid $ListenPort
    if ($nowPid -gt 0 -and $nowPid -ne $OldPid) {
      if ((Get-HttpStatus $ListenPort) -eq 200) { return $nowPid }
    }
    Start-Sleep -Milliseconds 500
  }
  return 0
}

# 拆 "解释器 参数..."；路径带空格时用引号包住
function Split-CommandLine([string]$CommandLine) {
  if ([string]::IsNullOrWhiteSpace($CommandLine)) { return $null }
  $m = [regex]::Match($CommandLine, '^\s*(?:"([^"]+)"|([^\s]+))(?:\s+(.*))?$')
  if (-not $m.Success) { return $null }
  $exe = ''
  if ($m.Groups[1].Success) { $exe = $m.Groups[1].Value } else { $exe = $m.Groups[2].Value }
  $argLine = ''
  if ($m.Groups[3].Success) { $argLine = $m.Groups[3].Value }
  return [pscustomobject]@{ exe = $exe; args = $argLine }
}

# 优先让 run_bot.py 自己看 data\bot.stop 收尾；不行再强杀
function Stop-ProcessGracefully([int]$ProcId, [string]$CommandLine, [int]$GraceSeconds) {
  $botStopFile = Join-Path $DataDir 'bot.stop'
  $asked = $false
  if ($CommandLine -and $CommandLine -match 'run_bot\.py') {
    Write-Step '发现 run_bot.py：写 data\bot.stop，让它自己收尾'
    Set-Content -LiteralPath $botStopFile -Value ((Get-Date).ToString('yyyy-MM-dd HH:mm:ss')) -Encoding ascii
    $asked = $true
    $until = (Get-Date).AddSeconds($GraceSeconds)
    while ((Get-Date) -lt $until -and (Test-Alive $ProcId)) { Start-Sleep -Milliseconds 500 }
    Remove-Item -LiteralPath $botStopFile -ErrorAction SilentlyContinue
  }
  if (Test-Alive $ProcId) {
    if ($asked) { Write-Warn2 ('等了 ' + $GraceSeconds + ' 秒还没退出，强制结束 pid=' + $ProcId) }
    else { Write-Step ('结束进程 pid=' + $ProcId) }
    Stop-Process -Id $ProcId -Force -ErrorAction SilentlyContinue
    $until = (Get-Date).AddSeconds(10)
    while ((Get-Date) -lt $until -and (Test-Alive $ProcId)) { Start-Sleep -Milliseconds 300 }
  }
}

# 用 WMI 创建（父进程是 WmiPrvSE，天然脱离当前终端的作业对象），输出重定向到日志
function Start-ConsoleProcess([string]$ExePath, [string]$Arguments, [string]$LogPath) {
  $launchExe = $ExePath
  if ($launchExe -match '(?i)python\.exe$') {
    $candidate = ($launchExe -replace '(?i)python\.exe$', 'pythonw.exe')
    if (Test-Path -LiteralPath $candidate) { $launchExe = $candidate }
  }
  if (-not (Test-Path -LiteralPath $launchExe)) { throw ('解释器不存在：' + $launchExe) }
  New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
  $inner = '"' + $launchExe + '" ' + $Arguments + ' >> "' + $LogPath + '" 2>&1'
  $cmd = 'cmd.exe /c "' + $inner + '"'
  try {
    $result = ([wmiclass]'Win32_Process').Create($cmd, $Root)
    if ($result.ReturnValue -eq 0) { return }
    throw ('Win32_Process.Create 返回 ' + $result.ReturnValue)
  } catch {
    Write-Warn2 ('WMI 启动失败（' + $_.Exception.Message + '），退回 Start-Process（会留在当前终端的作业对象里）')
    Start-Process -FilePath $launchExe -ArgumentList $Arguments -WorkingDirectory $Root -WindowStyle Hidden | Out-Null
  }
}

function Show-LogTail {
  foreach ($name in @('supervisor.log', 'bot.console.log')) {
    $path = Join-Path $LogDir $name
    if (-not (Test-Path -LiteralPath $path)) { continue }
    Write-Host ('--- ' + $name + '（最后 10 行）---') -ForegroundColor DarkGray
    Get-Content -LiteralPath $path -Tail 10 -Encoding UTF8 -ErrorAction SilentlyContinue
  }
}

function Show-Status {
  $listenPid = Get-ListenPid $Port
  $sup = Get-SupervisorInfo
  if ($listenPid -gt 0) {
    Write-Host ('端口 127.0.0.1:' + $Port + '：监听中 pid=' + $listenPid)
  } else {
    Write-Host ('端口 127.0.0.1:' + $Port + '：没有监听')
  }
  if ($sup.alive) {
    $restarts = 0
    if ($sup.state -and $null -ne $sup.state.restarts) { $restarts = [int]$sup.state.restarts }
    Write-Host ('守护：pid=' + $sup.pid + ' 活着，child_pid=' + $sup.child_pid + '，已重启 ' + $restarts + ' 次')
  } else {
    Write-Host '守护：没有在跑'
  }
  if ($listenPid -gt 0) {
    $parts = @()
    foreach ($p in (Get-Chain $listenPid)) { $parts += ([string]$p.ProcessId + '(' + [string]$p.Name + ')') }
    Write-Host ('进程链：' + ($parts -join ' <- '))
    $mode = Get-ConsoleMode $listenPid
    if ($mode -eq 'supervised') {
      Write-Host '模式：守护托管（重启写 data\restart.request，守护不重启）'
    } else {
      Write-Host '模式：独立进程（重启 = 结束进程后按原命令行重新拉起）'
    }
    $code = Get-HttpStatus $Port
    if ($code -gt 0) { Write-Host ('HTTP：' + $code) } else { Write-Host 'HTTP：无响应' -ForegroundColor Yellow }
  }
  Write-Host ('日志：' + (Join-Path $LogDir 'supervisor.log') + '、' + $ConsoleLog)
}

function Restart-Supervised([int]$ListenPid, $Sup) {
  $started = Get-Date
  $deadline = $started.AddSeconds($TimeoutSec)
  $oldChild = [int]$Sup.child_pid
  if ($Force) {
    Write-Warn2 ('-Force：直接结束监听进程 pid=' + $ListenPid + '，交给守护按「崩溃重启」拉起（会计入重启次数）')
    Stop-Process -Id $ListenPid -Force -ErrorAction SilentlyContinue
  } else {
    New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
    Set-Content -LiteralPath $RestartFile -Value ((Get-Date).ToString('yyyy-MM-dd HH:mm:ss')) -Encoding ascii
    Write-Step ('已写 ' + $RestartFile)
    Write-Step ('守护 pid=' + $Sup.pid + ' 会让主进程优雅退出后立即重启（不计崩溃次数；守护自己不动）')
    if ($oldChild -gt 0) { Write-Step ('旧 child_pid=' + $oldChild + '，等它换成新值') }
  }
  $newPid = Wait-Console $Port $ListenPid $deadline
  if ($newPid -le 0) {
    if (Test-Path -LiteralPath $RestartFile) {
      throw ('超时：' + $TimeoutSec + ' 秒内没等到新进程，data\restart.request 还在（守护可能没在跑）；看 logs\supervisor.log')
    }
    throw ('超时：' + $TimeoutSec + ' 秒内 127.0.0.1:' + $Port + ' 没恢复；看 logs\supervisor.log 与 logs\bot.console.log')
  }
  $after = Get-SupervisorInfo
  $elapsed = [int]((Get-Date) - $started).TotalSeconds
  $supNote = '守护 pid=' + $after.pid
  if ($after.pid -ne $Sup.pid) { $supNote = '守护换了 pid=' + $after.pid + '（原本 ' + $Sup.pid + '）' } else { $supNote = $supNote + '（未重启）' }
  Write-Ok ('管理台已重启：新 pid=' + $newPid + '，child_pid=' + $after.child_pid + '，' + $supNote + '，耗时 ' + $elapsed + ' 秒')
  if ($Force -and $after.state -and $null -ne $after.state.restarts) {
    Write-Host ('  （-Force 走崩溃重启，守护累计重启次数=' + $after.state.restarts + '）') -ForegroundColor DarkGray
  }
}

function Restart-Standalone([int]$ListenPid, [string]$CommandLine) {
  if ($CommandLine -match 'run_livedeck') {
    throw ('pid=' + $ListenPid + ' 是多屏直播监控台（livedeck），不是 Web 管理台；它不归本脚本重启')
  }
  $parsed = Split-CommandLine $CommandLine
  $exe = ''
  $argLine = ''
  if ($parsed) { $exe = $parsed.exe; $argLine = $parsed.args }
  if (-not $exe -or -not (Test-Path -LiteralPath $exe)) {
    $exe = Get-PythonPath
    $argLine = '"' + (Join-Path $Root 'scripts\run_console.py') + '" --host 127.0.0.1 --port ' + $Port + ' --db data\bot.db --log-file logs\bot.log'
    Write-Warn2 '拿不到原命令行，按 run_console.py 的默认参数重启'
  }
  if ($argLine -notmatch '(?i)run_(bot|console)\.py') {
    throw ('监听进程 pid=' + $ListenPid + ' 的命令行不像管理台：' + $CommandLine)
  }
  Write-Step '独立模式（不在守护下面）：按原命令行重启'
  Write-Host ('  ' + $exe + ' ' + $argLine) -ForegroundColor DarkGray
  $started = Get-Date
  Stop-ProcessGracefully $ListenPid $CommandLine $GraceSec
  $until = (Get-Date).AddSeconds(10)
  while ((Get-Date) -lt $until -and (Get-ListenPid $Port) -gt 0) { Start-Sleep -Milliseconds 300 }
  if ((Get-ListenPid $Port) -gt 0) { throw ('旧进程还占着端口 ' + $Port + '，没有重启') }
  Start-ConsoleProcess $exe $argLine $ConsoleLog
  $newPid = Wait-Console $Port 0 ($started.AddSeconds($TimeoutSec))
  if ($newPid -le 0) { throw ('等了 ' + $TimeoutSec + ' 秒，127.0.0.1:' + $Port + ' 还没起来；看 ' + $ConsoleLog) }
  $elapsed = [int]((Get-Date) - $started).TotalSeconds
  Write-Ok ('管理台已重启：新 pid=' + $newPid + '，耗时 ' + $elapsed + ' 秒；输出在 ' + $ConsoleLog)
}

try {
  if ($Status) {
    Show-Status
    exit 0
  }

  if ($Port -lt 1 -or $Port -gt 65535) { throw ('端口不合法：' + $Port) }

  $listenPid = Get-ListenPid $Port
  if ($listenPid -le 0) {
    Write-Warn2 ('127.0.0.1:' + $Port + ' 上没有进程在监听，管理台没在跑')
    if (-not $Start) {
      throw '没有可重启的进程。要单独起一个管理台，加 -Start；要连守护一起起，用 scripts\service.ps1 start'
    }
    $py = Get-PythonPath
    $consoleScript = Join-Path $Root 'scripts\run_console.py'
    $argLine = '"' + $consoleScript + '" --host 127.0.0.1 --port ' + $Port + ' --db data\bot.db --log-file logs\bot.log'
    Write-Step ('启动管理台：' + $py + ' ' + $argLine)
    Start-ConsoleProcess $py $argLine $ConsoleLog
    $newPid = Wait-Console $Port 0 ((Get-Date).AddSeconds($TimeoutSec))
    if ($newPid -le 0) { throw ('等了 ' + $TimeoutSec + ' 秒，127.0.0.1:' + $Port + ' 还没起来；看 ' + $ConsoleLog) }
    Write-Ok ('管理台已启动 pid=' + $newPid)
    exit 0
  }

  $info = Get-ProcessInfo $listenPid
  $procName = '未知'
  $cmdLine = ''
  if ($info) {
    $procName = [string]$info.Name
    $cmdLine = [string]$info.CommandLine
  }
  Write-Step ('当前监听 127.0.0.1:' + $Port + '：pid=' + $listenPid + '（' + $procName + '）')

  $mode = Get-ConsoleMode $listenPid
  if ($mode -eq 'supervised') {
    $sup = Get-SupervisorInfo
    if ($sup.pid -le 0 -or -not $sup.alive) {
      throw '进程在守护树里，但 data\supervisor.json 里读不到活着的守护；请用 pwsh -File scripts\service.ps1 restart'
    }
    Restart-Supervised $listenPid $sup
    exit 0
  }

  if ($procName -notmatch '(?i)python') {
    throw ('端口 ' + $Port + ' 被 ' + $procName + '（pid=' + $listenPid + '）占用，不是管理台；确认端口，或用 -Port 指定')
  }
  if ($cmdLine -and $cmdLine -match 'run_livedeck') {
    throw ('端口 ' + $Port + ' 上是多屏直播监控台（livedeck），不是 Web 管理台；它不归本脚本重启')
  }
  Restart-Standalone $listenPid $cmdLine
  exit 0
} catch {
  Write-Host ('[失败] ' + $_.Exception.Message) -ForegroundColor Red
  Show-LogTail
  exit 1
}
