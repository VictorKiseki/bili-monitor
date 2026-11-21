$ErrorActionPreference = "Stop"
$project = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $project "runtime\pythonw.exe"
if (-not (Test-Path -LiteralPath $pythonw)) {
    $pythonw = Join-Path $project ".venv\Scripts\pythonw.exe"
}
if (-not (Test-Path -LiteralPath $pythonw)) {
    $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($cmd) { $pythonw = $cmd.Source }
}
$cleanup = Join-Path $project "scripts\recorder_cleanup.py"
$config = Join-Path $project "config\recorder_cleanup.json"
$startup = [Environment]::GetFolderPath("Startup")
$lnkPath = Join-Path $startup "BililiveRecorderCleanup.lnk"

if (-not (Test-Path -LiteralPath $pythonw)) { throw "pythonw not found: $pythonw" }
if (-not (Test-Path -LiteralPath $cleanup)) { throw "cleanup script not found: $cleanup" }

# 间隔由 config/recorder_cleanup.json 的 interval_s 决定，快捷方式不再写死 --interval
$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut($lnkPath)
$sc.TargetPath = $pythonw
$sc.Arguments = '"' + $cleanup + '" --loop'
$sc.WorkingDirectory = $project
$sc.WindowStyle = 7
$sc.Description = "BililiveRecorder cleanup daemon"
$sc.Save()

# 管理台关闭自动清理时，只保留登录快捷方式，不拉起守护
$enabled = $true
if (Test-Path -LiteralPath $config) {
    try {
        $cfg = Get-Content -LiteralPath $config -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($null -ne $cfg.enabled) {
            if ($cfg.enabled -is [string]) {
                $enabled = @("1", "true", "yes", "on") -contains $cfg.enabled.ToLower()
            } else {
                $enabled = [bool]$cfg.enabled
            }
        }
    } catch {
        "WARN_CONFIG=$($_.Exception.Message)"
    }
}
if (-not $enabled) {
    "SHORTCUT=$lnkPath"
    "AUTOSTART_DISABLED"
    return
}

$existing = @(Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -like "*recorder_cleanup.py*" })
if ($existing.Count -eq 0) {
    $cmd = '"' + $pythonw + '" "' + $cleanup + '" --loop'
    $res = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
        CommandLine = $cmd
        CurrentDirectory = $project
    }
    "STARTED_PID=$($res.ProcessId) RETURN=$($res.ReturnValue)"
} else {
    "ALREADY_RUNNING_PID=$(($existing | ForEach-Object ProcessId) -join ',')"
}
"SHORTCUT=$lnkPath"
