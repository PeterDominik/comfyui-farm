<#
.SYNOPSIS
  Starts this machine's agent when you log in, without admin rights: a shortcut in your
  Startup folder runs deploy\windows\run_agent.ps1 in a hidden window, which restarts the
  agent if it exits. (With admin rights, install_services.ps1 runs it as a service instead.)

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File deploy\windows\install_startup.ps1 -StartNow
  powershell -ExecutionPolicy Bypass -File deploy\windows\install_startup.ps1 -Stop      # stop it now
  powershell -ExecutionPolicy Bypass -File deploy\windows\install_startup.ps1 -Remove    # no longer at logon

.NOTES
  It runs while you're logged in (locking the screen is fine; logging out stops it).
  Stopping the agent leaves ComfyUI and its jobs running.
#>
param(
    [string]$Config = "",          # agent.yaml (default: the repo's)
    [switch]$StartNow,             # also start it now
    [switch]$Stop,                 # stop the running agent (and its runner)
    [switch]$Remove,               # remove the shortcut (and stop it)
    [string]$StartupDir = ""       # default: your Startup folder (tests use another)
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)   # the repo
if (-not $StartupDir) { $StartupDir = [Environment]::GetFolderPath("Startup") }
$link = Join-Path $StartupDir "ComfyFarm agent.lnk"
$runner = Join-Path $PSScriptRoot "run_agent.ps1"
$psArgs = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$runner`""
if ($Config) { $psArgs += " -Config `"$Config`"" }

function Stop-Agent {
    foreach ($name in "agent-runner.pid", "agent.pid") {
        $f = Join-Path $Root "logs\$name"
        if (Test-Path $f) {
            $id = [int](Get-Content $f -TotalCount 1)
            $p = Get-Process -Id $id -ErrorAction SilentlyContinue
            if ($p) { Stop-Process -Id $id -Force; Write-Host "Stopped $name ($id)." }
            Remove-Item $f -ErrorAction SilentlyContinue
        }
    }
}

if ($Stop -or $Remove) { Stop-Agent }
if ($Remove) {
    if (Test-Path $link) { Remove-Item $link; Write-Host "Removed ${link}: the agent no longer starts at logon." }
    else { Write-Host "No $link to remove." }
    exit 0
}
if ($Stop -and -not $StartNow) { exit 0 }      # -Stop -StartNow: a restart

$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($link)
$sc.TargetPath = (Get-Command powershell.exe).Source
$sc.Arguments = $psArgs
$sc.WorkingDirectory = $Root
$sc.WindowStyle = 7                 # minimized: the script hides its window itself
$sc.Description = "ComfyFarm agent (deploy\windows\run_agent.ps1)"
$sc.Save()
Write-Host "The agent now starts when you log in (${link})."

if ($StartNow) {
    Start-Process -FilePath (Get-Command powershell.exe).Source -ArgumentList $psArgs -WindowStyle Hidden
    Write-Host "Started. Its log: $Root\logs\agent-<name>.log (and logs\agent-console.log)"
}
