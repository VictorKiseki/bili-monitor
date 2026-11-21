<#
  B 站监控：Windows 开机自启 / 守护 一键脚本

  用法（在项目根目录执行，pwsh -File scripts\service.ps1 <动作>）：
    status      看当前装了哪种自启方式、守护是否在跑（默认动作）
    install     安装开机自启（优先计划任务；没有管理员权限时自动降级到启动文件夹）
    start       立刻启动（不等下次登录）—— 用 WMI 创建进程，脱离当前终端的作业对象
    stop        优雅停止（写停止文件 → 等它收尾 → 兜底强杀）
    restart     重启
    uninstall   取消开机自启（保留 data 与 logs）
    logs        看最近日志
    autostart-status / autostart-on / autostart-off  进程页调用，只读写登录自启项

  可选参数：
    -TaskName     任务名（默认 BiliMonitor）
    -ConsoleArgs  传给 run_console.py 的额外参数（一般不用给）
    -BotArgs      旧参数，已废弃（保留只为不打断旧命令；给了会提示忽略）
    -Python       python 完整路径（默认自动找）
    -Port         管理台端口（默认 8765）

  结构：自启项 → scripts\service_start.cmd → scripts\supervisor.py
        → scripts\run_console.py（监控台主进程，只负责页面与托管）
             ├─ scripts\run_bot.py --collector（采集进程：绑上 B 站账号后自动起）
             └─ scripts\run_pusher.py        （推送进程：绑上 QQ 机器人后自动起）
        守护进程负责崩溃重启、卡死重启、日志轮转、单实例；采集/推送由主进程托管，
        页面服务不会被它们拖住。
#>
param(
  [ValidateSet('install', 'uninstall', 'start', 'stop', 'restart', 'status', 'logs',
               'autostart-status', 'autostart-on', 'autostart-off')]
  [string]$Action = 'status',
  [string]$TaskName = 'BiliMonitor',
  [string]$BotArgs = '',
  [string]$ConsoleArgs = '',
  [string]$Python = '',
  [int]$Port = 8765
)

$ErrorActionPreference = 'Stop'
$env:PYTHONIOENCODING = 'utf-8'   # 让 supervisor.py 的 --status 中文输出不再乱码
$Root = Split-Path -Parent $PSScriptRoot
$CmdPath = Join-Path $Root 'scripts\service_start.cmd'
$VbsPath = Join-Path $Root 'scripts\service_hidden.vbs'
$StateFile = Join-Path $Root 'data\supervisor.json'
$LogDir = Join-Path $Root 'logs'
$StartupDir = [Environment]::GetFolderPath('Startup')
$StartupFile = Join-Path $StartupDir ($TaskName + '.vbs')

function Get-PythonPath {
  if ($Python) { return $Python }
  # 优先项目自带运行时（runtime\，解压即用），其次开发机的 .venv，最后 PATH 上的 python / py
  $rtPy = Join-Path $Root 'runtime\python.exe'
  if (Test-Path -LiteralPath $rtPy) { return $rtPy }
  $venvPy = Join-Path $Root '.venv\Scripts\python.exe'
  if (Test-Path -LiteralPath $venvPy) { return $venvPy }
  $cmd = Get-Command python -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  $cmd = Get-Command python3 -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  $py = Get-Command py -ErrorAction SilentlyContinue
  if ($py) { return $py.Source }
  throw '找不到 python，请先运行 install.cmd（自带运行时在 runtime\），或用 -Python 指定完整路径'
}

function Get-AutostartKind {
  $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  if ($task -and $task.Settings.Enabled) { return 'task' }
  if (Test-Path $StartupFile) { return 'startup' }
  return 'none'
}

function Initialize-JobApi {
  if ('W.Job' -as [type]) { return }
  Add-Type -Namespace W -Name Job -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("kernel32.dll", SetLastError = true)] public static extern System.IntPtr OpenProcess(uint access, bool inherit, int pid);
[System.Runtime.InteropServices.DllImport("kernel32.dll")] public static extern bool IsProcessInJob(System.IntPtr h, System.IntPtr j, out bool r);
[System.Runtime.InteropServices.DllImport("kernel32.dll")] public static extern bool CloseHandle(System.IntPtr h);
'@
}

function Get-JobState([int]$ProcId) {
  Initialize-JobApi
  $h = [W.Job]::OpenProcess(0x1000, $false, $ProcId)
  if ($h -eq [System.IntPtr]::Zero) { return $null }
  $inJob = $false
  [void][W.Job]::IsProcessInJob($h, [System.IntPtr]::Zero, [ref]$inJob)
  [void][W.Job]::CloseHandle($h)
  return $inJob
}

function Get-ProcBrief([int]$ProcId) {
  $p = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $ProcId) -ErrorAction SilentlyContinue
  if (-not $p) { return $null }
  return @{ pid = [int]$p.ProcessId; ppid = [int]$p.ParentProcessId; name = [string]$p.Name }
}

# 判断「关掉终端 / DSH 会不会连坐」。
# 自带运行时（runtime\）下 python.exe / pythonw.exe 就是解释器本体，没有「外层启动器」这一层：
# 进程本身在不在调用方的作业对象里，就是最终答案（WMI 创建 → 不在；Start-Process → 在）。
# 开发机的 .venv\Scripts\python.exe 是 CPython 的启动器（venvlauncher），它会自己建一个作业把
# 真解释器塞进去（回收子进程用），这时真解释器永远「在作业里」，需要再看一层外层启动器。
function Test-VenvLauncher([int]$ProcId) {
  $p = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $ProcId) -ErrorAction SilentlyContinue
  if (-not $p) { return $false }
  return ([string]$p.ExecutablePath -like '*.venv\Scripts\*')
}

function Show-JobState([int]$ProcId) {
  $s = Get-JobState $ProcId
  if ($null -eq $s) { Write-Host ('  pid ' + $ProcId + ' 已退出'); return }
  if (-not $s) {
    Write-Host ('  pid ' + $ProcId + ' [OK] 不在作业对象里：关掉终端/DSH 不受影响')
    return
  }
  if (Test-VenvLauncher $ProcId) {
    $info = Get-ProcBrief $ProcId
    if ($info -and $info.ppid -gt 0) {
      $ps = Get-JobState ([int]$info.ppid)
      $pi = Get-ProcBrief ([int]$info.ppid)
      if ($null -ne $ps -and -not $ps -and $pi) {
        Write-Host ('  pid ' + $ProcId + ' [OK] 在作业对象里，但外层启动器 pid ' + $info.ppid + '（' + $pi.name + '）不在任何作业里')
        Write-Host '        这个作业是 venv 启动器回收子进程用的；关掉终端/DSH 不会连坐'
        return
      }
    }
  }
  Write-Host ('  pid ' + $ProcId + ' [警告] 在作业对象里：关掉启动它的终端/DSH 会被连带终止') -ForegroundColor Yellow
  Write-Host '        补救：用 service.ps1 start（WMI 创建，脱离作业对象），或重新登录让开机自启接管'
}

function Start-Detached([string]$py, [string]$supArgs) {
  # 目标：新进程不能落在调用方的作业对象里。DSH 的 pwsh 带 JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE，
  # 从它 Start-Process 出来的一切都在同一个作业里，宿主一退全部连坐。
  # 做法：让 WMI 的 Win32_Process.Create 帮忙创建（父进程是 WmiPrvSE.exe，天然脱离本作业）。
  # 反例（实测）：WMI 起 wscript.exe、再让 VBS 用 WScript.Shell.Run 拉 pythonw，会被静默拒绝，一个进程都起不来。
  Initialize-JobApi
  $pwx = $py -replace 'python\.exe$', 'pythonw.exe'
  if (-not (Test-Path $pwx)) { $pwx = $py }
  $cmd = '"' + $pwx + '" ' + $supArgs
  try {
    $r = ([wmiclass]'Win32_Process').Create($cmd, $Root)
    if ($r.ReturnValue -eq 0) {
      Write-Host ('[启动] WMI 已创建守护进程，pid=' + $r.ProcessId + '（脱离作业对象）')
      return $true
    }
    Write-Host ('[启动] WMI 创建失败，ReturnValue=' + $r.ReturnValue)
  } catch {
    Write-Host ('[启动] WMI 创建异常：' + $_.Exception.Message)
  }
  Write-Host '[启动] 退回普通 Start-Process（该进程仍在本终端作业对象里）'
  Start-Process -FilePath $pwx -ArgumentList $supArgs -WorkingDirectory $Root -WindowStyle Hidden
  return $false
}

function Get-SupervisorArgs([string]$py) {
  # 主进程 = run_console.py：只起页面 + 托管采集/推送；这两条要不要跑看账号绑没绑
  $console = 'scripts\run_console.py --port ' + $Port
  if ($ConsoleArgs) { $console = $console + ' ' + $ConsoleArgs }
  return 'scripts\supervisor.py --log-dir logs --db data\bot.db --state-file data\supervisor.json --stop-file data\supervisor.stop --child-stop-file data\console.stop --worker-stop-file data\schedule_worker.stop --restart-file data\restart.request -- "' + $py + '" ' + $console
}

function Get-LoginTask {
  $scheduler = New-Object -ComObject Schedule.Service
  $scheduler.Connect()
  $folder = $scheduler.GetFolder('\')
  try {
    return $folder.GetTask($TaskName)
  } catch {
    # HRESULT 0x80070002 表示任务不存在；权限错误不能当成未配置。
    if ($_.Exception.GetBaseException().HResult -ne -2147024894) { throw }
  }
}

function Update-LoginAutostart {
  if (-not $StartupDir -or -not (Test-Path -LiteralPath $StartupDir -PathType Container)) {
    throw '找不到当前用户的启动文件夹'
  }
  $task = Get-LoginTask
  if ($task) {
    $actions = $task.Definition.Actions
    if ($actions.Count -ne 1 -or $actions.Item(1).Path -ne $CmdPath) { return 'conflict' }
  }
  $hasStartup = Test-Path -LiteralPath $StartupFile
  $workingDir = 'sh.CurrentDirectory = "' + $Root + '"'
  $createShell = 'Set sh = CreateObject("WScript.Shell")'
  $startupHeader = "' bili monitor login launcher"
  $legacyVbs = @("' bili monitor hidden launcher", $createShell, $workingDir,
                 ('sh.Run """' + $CmdPath + '""", 0, False'))
  $legacyText = $legacyVbs -join [Environment]::NewLine
  if ($hasStartup) {
    $lines = @(Get-Content -LiteralPath $StartupFile)
    $owned = $lines.Count -eq 4 -and $lines[0] -eq $startupHeader -and $lines[2] -eq $workingDir
    if (-not $owned -and ($lines -join [Environment]::NewLine) -ne $legacyText) { return 'conflict' }
  }
  $kind = if ($task -and $task.Enabled) { 'task' } elseif ($hasStartup) { 'startup' } else { 'none' }
  if ($Action -eq 'autostart-status') { return $kind }
  if ($Action -eq 'autostart-off') {
    if ($task -and $task.Enabled) { $task.Enabled = $false }
    if ($hasStartup) { Remove-Item -LiteralPath $StartupFile -ErrorAction Stop }
    return 'none'
  }
  if ($kind -ne 'none') { return $kind }
  if ($task) {
    $task.Enabled = $true
    return 'task'
  }
  if ((Test-Path -LiteralPath $VbsPath -PathType Leaf) -and
      ((Get-Content -LiteralPath $VbsPath) -join [Environment]::NewLine) -eq $legacyText) {
    # 恢复 service.ps1 install 生成的启动器，保留其端口和额外参数。
    $vbs = $legacyVbs
  } else {
    # 首次从页面设置自启时使用当前管理台端口。
    $py = Get-PythonPath
    $consolePython = $py -replace 'pythonw\.exe$', 'python.exe'
    if (Test-Path -LiteralPath $consolePython -PathType Leaf) { $py = $consolePython }
    $pythonw = $py -replace 'python\.exe$', 'pythonw.exe'
    if (-not (Test-Path -LiteralPath $pythonw -PathType Leaf)) { $pythonw = $py }
    $command = '"' + $pythonw + '" ' + (Get-SupervisorArgs $py)
    $vbs = @($startupHeader, $createShell, $workingDir,
             ('sh.Run "' + $command.Replace('"', '""') + '", 0, False'))
  }
  # 同名文件已存在时不覆盖；写入失败时清理新建的文件，允许重试。
  $file = [IO.File]::Open($StartupFile, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write)
  $written = $false
  try {
    $writer = [IO.StreamWriter]::new($file, [Text.Encoding]::Unicode)
    try { $writer.WriteLine(($vbs -join [Environment]::NewLine)) } finally { $writer.Dispose() }
    $written = $true
  } finally {
    $file.Dispose()
    if (-not $written) { Remove-Item -LiteralPath $StartupFile -ErrorAction SilentlyContinue }
  }
  return 'startup'
}

function New-Launchers([string]$py) {
  $supArgs = Get-SupervisorArgs $py
  $cmdLines = @(
    '@echo off',
    'chcp 65001 >nul',
    ('cd /d "' + $Root + '"'),
    'set PYTHONIOENCODING=utf-8',
    'set PYTHONUNBUFFERED=1',
    ('"' + $py + '" ' + $supArgs),
    'exit /b %ERRORLEVEL%'
  )
  Set-Content -Path $CmdPath -Value $cmdLines -Encoding ascii

  $vbsLines = @(
    "' bili monitor hidden launcher",
    'Set sh = CreateObject("WScript.Shell")',
    ('sh.CurrentDirectory = "' + $Root + '"'),
    ('sh.Run """' + $CmdPath + '""", 0, False')
  )
  Set-Content -Path $VbsPath -Value $vbsLines -Encoding ascii
  Write-Host ('已写入启动器：' + $CmdPath)
  Write-Host ('已写入隐藏启动器：' + $VbsPath)
}

function Show-Status {
  Write-Host '=== 开机自启 ==='
  $kind = Get-AutostartKind
  if ($kind -eq 'task') {
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Host ('方式：计划任务 "' + $TaskName + '"（状态 ' + (Get-ScheduledTask -TaskName $TaskName).State + '）')
    Write-Host ('上次运行：' + $info.LastRunTime + '  结果：' + $info.LastTaskResult + '  下次：' + $info.NextRunTime)
  } elseif ($kind -eq 'startup') {
    Write-Host ('方式：启动文件夹（' + $StartupFile + '）')
    Write-Host '说明：登录后自动启动；没有管理员权限时用这个'
  } else {
    Write-Host '未安装（登录后不会自动启动）'
  }
  Write-Host ''
  Write-Host '=== 守护进程 ==='
  $py = Get-PythonPath
  & $py (Join-Path $Root 'scripts\supervisor.py') --status --state-file $StateFile
  if (Test-Path $StateFile) {
    $st = Get-Content $StateFile -Raw -Encoding utf8 | ConvertFrom-Json
    if ($st -and $st.pid) {
      Write-Host ''
      Write-Host '=== 进程归属（作业对象）==='
      Show-JobState ([int]$st.pid)
      if ($st.child_pid) { Show-JobState ([int]$st.child_pid) }
      if ($st.worker_pid) { Show-JobState ([int]$st.worker_pid) }
    }
  }
  Write-Host ''
  Write-Host '=== 采集 / 推送进程 ==='
  $stackFile = Join-Path $Root 'data\stack.json'
  if (Test-Path $stackFile) {
    try {
      $stk = Get-Content $stackFile -Raw -Encoding utf8 | ConvertFrom-Json
      foreach ($role in @('collector', 'pusher')) {
        $info = $stk.roles.$role
        if (-not $info) { continue }
        $state = if ($info.alive) { '运行中 pid=' + $info.pid } else { '未运行' }
        Write-Host ('  ' + $info.label + '：' + $state + '（想要=' + $info.wanted + '，重启 ' + $info.restarts + ' 次）')
        if ($info.last_error) { Write-Host ('    最近错误：' + $info.last_error) }
      }
      $bs = $stk.bindings
      if ($bs) {
        Write-Host ('  绑定状态：B 站 ' + $(if ($bs.bili.bound) { '已绑定' } else { '未绑定' }) + ' / QQ ' + $(if ($bs.qq.bound) { '已绑定' } else { '未绑定' }))
      }
    } catch {
      Write-Host ('  读 data\stack.json 失败：' + $_.Exception.Message)
    }
  } else {
    Write-Host '  还没有栈状态（主进程没起过，或起的是老的 run_bot.py --web）'
  }
  Write-Host ''
  Write-Host '=== 管理台端口 ==='
  $listen = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
  if ($listen) {
    Write-Host ('正在监听 127.0.0.1:' + $Port + '  → http://127.0.0.1:' + $Port)
  } else {
    Write-Host ('端口 ' + $Port + ' 没有监听（管理台没起）')
  }
}

function Stop-Stray {
  $procs = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue
  foreach ($p in $procs) {
    $cl = [string]$p.CommandLine
    if ($cl -and $cl.Contains($Root) -and ($cl.Contains('scripts\supervisor.py') -or $cl.Contains('scripts/run_bot.py') -or $cl.Contains('scripts/run_pusher.py') -or $cl.Contains('scripts/run_console.py') -or $cl.Contains('scripts\schedule_worker.py'))) {
      Write-Host ('清理残留进程 pid=' + $p.ProcessId)
      Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
  }
}

function Invoke-Stop {
  $py = Get-PythonPath
  if (Test-Path $StateFile) {
    & $py (Join-Path $Root 'scripts\supervisor.py') --stop --state-file $StateFile --stop-file (Join-Path $Root 'data\supervisor.stop') --child-stop-file (Join-Path $Root 'data\console.stop')
  } else {
    Write-Host '没有状态文件，守护可能本来就没跑'
  }
  if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    $t = Get-ScheduledTask -TaskName $TaskName
    if ($t.State -eq 'Running') {
      Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
      Write-Host '已结束计划任务'
    }
  }
  Stop-Stray
  Write-Host '已停止'
}

try {
  if ($Action -like 'autostart-*') {
    Update-LoginAutostart
    exit 0
  }
  if ($BotArgs) {
    Write-Host ('[提示] -BotArgs 已弃用，忽略：' + $BotArgs) -ForegroundColor Yellow
    Write-Host '       主进程现在是 run_console.py（只起页面并托管采集/推送），不再直接跑 run_bot.py' -ForegroundColor Yellow
  }
  switch ($Action) {
    'install' {
      $py = Get-PythonPath
      New-Launchers $py
      $installed = $false
      try {
        $taskAction = New-ScheduledTaskAction -Execute $CmdPath -WorkingDirectory $Root
        $trigger = New-ScheduledTaskTrigger -AtLogOn
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
        $principal = New-ScheduledTaskPrincipal -UserId ("$env:USERDOMAIN\$env:USERNAME") -LogonType Interactive -RunLevel Limited
        Register-ScheduledTask -TaskName $TaskName -Action $taskAction -Trigger $trigger -Settings $settings -Principal $principal -Description 'B 站主播监控 + QQ 通知（守护进程随登录启动）' -Force | Out-Null
        $installed = $true
        Write-Host ('已注册计划任务 "' + $TaskName + '"：登录时自动启动，崩溃由守护重启')
      } catch {
        Write-Host ('计划任务注册失败（' + $_.Exception.Message.Trim() + '），改为启动文件夹方案')
      }
      if (-not $installed) {
        # 直接写内容而不用 Copy-Item：新生成的 .vbs 有时会被杀软瞬时锁住导致复制失败
        $vbsText = Get-Content -Path $VbsPath -Raw -Encoding ascii
        $ok = $false
        for ($i = 1; $i -le 3; $i++) {
          try {
            Set-Content -Path $StartupFile -Value $vbsText -Encoding ascii -NoNewline
            $ok = $true
            break
          } catch {
            Write-Host ('写启动项失败（第 ' + $i + ' 次）：' + $_.Exception.Message)
            Start-Sleep -Seconds 2
          }
        }
        if (-not $ok) { throw '启动文件夹写入被拒绝（可能被杀软或系统策略拦截）' }
        Write-Host ('已安装到启动文件夹：' + $StartupFile)
        Write-Host '登录后会自动启动，不弹控制台窗口'
        Write-Host '想要系统级守护（进程退出后系统也重启、开机未登录也跑）：用管理员身份重跑 install'
      }
      Write-Host '现在可以执行：pwsh -File scripts\service.ps1 start'
    }
    'uninstall' {
      Invoke-Stop
      if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host ('已删除计划任务 "' + $TaskName + '"')
      }
      if (Test-Path $StartupFile) {
        Remove-Item $StartupFile -Force
        Write-Host ('已删除启动文件夹项：' + $StartupFile)
      }
      Write-Host '数据（data）与日志（logs）都保留了'
    }
    'start' {
      $kind = Get-AutostartKind
      $detached = $true
      if ($kind -eq 'task') {
        Stop-Stray
        Start-ScheduledTask -TaskName $TaskName
      } elseif ($kind -eq 'startup') {
        Stop-Stray
        $py = Get-PythonPath
        $detached = Start-Detached $py (Get-SupervisorArgs $py)
      } else {
        # 没装自启也能临时起：安装脚本就靠这条路径先把页面拉起来
        Stop-Stray
        $py = Get-PythonPath
        $detached = Start-Detached $py (Get-SupervisorArgs $py)
        Write-Host '未安装开机自启，已临时启动（重启电脑后不会自动起；要自启请执行 install）'
      }
      Start-Sleep -Seconds 6
      if (-not $detached) {
        Write-Host ''
        Write-Host '[注意] 这次没能脱离当前终端的作业对象，关掉这个终端/DSH 会把监控一起杀掉。' -ForegroundColor Yellow
        Write-Host '       补救：重新登录让自启接管，或双击 scripts\service_hidden.vbs 手动拉起。' -ForegroundColor Yellow
      }
      Show-Status
    }
    'stop' { Invoke-Stop }
    'restart' {
      Invoke-Stop
      Start-Sleep -Seconds 2
      & $PSCommandPath -Action start -TaskName $TaskName -Port $Port -Python $Python -ConsoleArgs $ConsoleArgs
    }
    'status' { Show-Status }
    'logs' {
      foreach ($f in @('supervisor.log', 'console.log', 'bot.log', 'collector.log', 'pusher.log')) {
        $p = Join-Path $LogDir $f
        if (Test-Path $p) {
          Write-Host ('=== ' + $f + ' （最后 15 行）===')
          Get-Content $p -Tail 15
          Write-Host ''
        }
      }
    }
  }
} catch {
  Write-Host ('[失败] ' + $_.Exception.Message) -ForegroundColor Red
  exit 1
}
