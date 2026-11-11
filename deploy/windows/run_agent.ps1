<#
.SYNOPSIS
  Runs this machine's agent and starts it again if it exits (install_startup.ps1 runs this
  at logon in a hidden window, or every 5 minutes from a scheduled task with -Always). One
  at a time, across login sessions: a second copy exits at once.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File deploy\windows\run_agent.ps1
  powershell -ExecutionPolicy Bypass -File deploy\windows\run_agent.ps1 -Config D:\comfyfarm\agent.yaml

.NOTES
  The agent logs to logs\agent-<name>.log (agent.yaml's log_dir); its console output goes to
  logs\agent-console.log. If agent.yaml has comfyui_launch, the agent starts ComfyUI too
  and keeps it running. Stop it all with:  deploy\windows\install_startup.ps1 -Stop

  run\drives.txt (written by install_startup.ps1 -Always) lists network drives as
  "B:=\\server\share": any that this session lacks (a scheduled task's has none) is mapped
  before the agent starts, so agent.yaml and ComfyUI can keep using B:\...
#>
param(
    [string]$Config = "",          # agent.yaml (default: the repo's)
    [int]$RestartSeconds = 10
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)   # the repo
Set-Location $Root
$py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "Run deploy\windows\setup.ps1 -Agent first." }
if (-not $Config) { $Config = Join-Path $Root "agent.yaml" }
$logDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "agent-console.log"

# One runner per agent.yaml on this machine, in any login session (the scheduled task's
# background session and yours share Global\).
$key = [BitConverter]::ToString([Security.Cryptography.SHA1]::Create().ComputeHash(
    [Text.Encoding]::UTF8.GetBytes(([IO.Path]::GetFullPath($Config)).ToLowerInvariant()))).Replace("-", "").Substring(0, 16)
try { $mutex = New-Object Threading.Mutex($false, "Global\ComfyFarmAgent-$key") }
catch { $mutex = New-Object Threading.Mutex($false, "Local\ComfyFarmAgent-$key") }
try { $mine = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $mine = $true }   # the last runner was killed
if (-not $mine) { Write-Host "The agent for $Config is already running."; exit 0 }

# Network drives this session lacks (a scheduled task's has none): mapped with this logon's
# own credentials, for this session only.
$drives = Join-Path $Root "run\drives.txt"
if (Test-Path $drives) {
    foreach ($line in Get-Content $drives) {
        $letter, $unc = $line -split "=", 2
        if (-not $letter -or -not $unc -or (Test-Path "$letter\")) { continue }
        $ErrorActionPreference = "Continue"
        $out = & net.exe use $letter $unc /persistent:no 2>&1 | ForEach-Object { "$_" }
        $ErrorActionPreference = "Stop"
        Add-Content -Path $log -Value "===== $(Get-Date -Format s) net use $letter $unc ($LASTEXITCODE): $($out -join ' ')"
    }
}

# The pipeline's Python settings must not leak into the farm's venv.
foreach ($v in "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
$env:PYTHONNOUSERSITE = "1"
$env:PYTHONUTF8 = "1"

Set-Content -Path (Join-Path $logDir "agent-runner.pid") -Value $PID
try {
    while ($true) {
        Add-Content -Path $log -Value "===== $(Get-Date -Format s) starting the agent ($Config)"
        $p = Start-Process -FilePath $py -ArgumentList @("-m", "comfyfarm", "agent", "--config", "`"$Config`"") `
            -WorkingDirectory $Root -NoNewWindow -PassThru `
            -RedirectStandardOutput "$log.out" -RedirectStandardError "$log.err"
        $null = $p.Handle               # keeps the exit code readable after it exits
        Set-Content -Path (Join-Path $logDir "agent.pid") -Value $p.Id
        $p.WaitForExit()
        foreach ($f in "$log.out", "$log.err") {
            if (Test-Path $f) { Get-Content $f -Tail 50 | Add-Content -Path $log }
        }
        Add-Content -Path $log -Value "===== $(Get-Date -Format s) the agent exited ($($p.ExitCode)); again in $RestartSeconds s"
        Start-Sleep -Seconds $RestartSeconds
    }
} finally {
    $mutex.ReleaseMutex()
}
