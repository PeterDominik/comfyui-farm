<#
.SYNOPSIS
  Keeps this machine's agent running without admin rights, in one of two ways:

    at logon (default)  a shortcut in your Startup folder runs deploy\windows\run_agent.ps1
                        in a hidden window when you log in. Locking the screen is fine;
                        signing out stops it, and after a reboot it waits for your login.
    -Always             a scheduled task runs it every 5 minutes whether or not you're
                        logged in (it does nothing while the agent runs), so it survives
                        sign-out and starts within 5 minutes of a reboot. Windows keeps your
                        password with the task: run -Always again after changing it.

  run_agent.ps1 restarts the agent if it exits, and only one runs per agent.yaml.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File deploy\windows\install_startup.ps1 -StartNow           # at logon
  powershell -ExecutionPolicy Bypass -File deploy\windows\install_startup.ps1 -Always -StartNow   # always
  powershell -ExecutionPolicy Bypass -File deploy\windows\install_startup.ps1 -Stop -StartNow     # restart (after git pull)
  powershell -ExecutionPolicy Bypass -File deploy\windows\install_startup.ps1 -Stop               # stop it
  powershell -ExecutionPolicy Bypass -File deploy\windows\install_startup.ps1 -Remove             # stop it, and neither any more
  powershell -ExecutionPolicy Bypass -File deploy\windows\install_startup.ps1 -AtLogon            # back from -Always to at logon

.NOTES
  -Stop and -StartNow keep whichever way is set up. With -Always, -Stop also pauses the task
  until -StartNow, so it isn't started again 5 minutes later.

  -Always needs your account to have the "Log on as a batch job" right on this machine.
  Without it, Windows refuses the task and this says so; IT can grant the right, or install
  the agent as a service (install_services.ps1). The task runs in a background session with
  no mapped drives: the network drives you have now (B: for /rdo/bank) are recorded in
  run\drives.txt, and run_agent.ps1 maps them there before starting the agent.

  Stopping the agent leaves ComfyUI and its jobs running.
#>
param(
    [string]$Config = "",          # agent.yaml (default: the repo's)
    [switch]$StartNow,             # also start it now
    [switch]$Stop,                 # stop the running agent (and its runner)
    [switch]$Remove,               # stop it, remove the shortcut and the task
    [switch]$Always,               # a scheduled task: runs whether or not you're logged in
    [switch]$AtLogon,              # a Startup-folder shortcut (the default), replacing the task
    [string]$StartupDir = ""       # default: your Startup folder (tests use another)
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)   # the repo
if (-not $StartupDir) { $StartupDir = [Environment]::GetFolderPath("Startup") }
$link = Join-Path $StartupDir "ComfyFarm agent.lnk"
$runner = Join-Path $PSScriptRoot "run_agent.ps1"
$psArgs = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$runner`""
if ($Config) { $psArgs += " -Config `"$Config`"" }
$powershell = (Get-Command powershell.exe).Source
$taskName = "ComfyFarm agent"
if ($Always -and $AtLogon) { throw "Pass -Always or -AtLogon, not both." }

function Get-OurTask {
    # The task, if it runs this checkout's run_agent.ps1 (a test checkout never sees the live one).
    $t = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($t -and ($t.Actions | Where-Object { $_.Arguments -and $_.Arguments.Contains($runner) })) { return $t }
    return $null
}

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

function Set-Shortcut {
    $shell = New-Object -ComObject WScript.Shell
    $sc = $shell.CreateShortcut($link)
    $sc.TargetPath = $powershell
    $sc.Arguments = $psArgs
    $sc.WorkingDirectory = $Root
    $sc.WindowStyle = 7                 # minimized: the script hides its window itself
    $sc.Description = "ComfyFarm agent (deploy\windows\run_agent.ps1)"
    $sc.Save()
    Write-Host "The agent now starts when you log in (${link})."
}

function Save-Drives {
    # The background session has no mapped drives: run_agent.ps1 maps these there.
    $drives = @(Get-CimInstance Win32_LogicalDisk -Filter "DriveType=4" |
        Where-Object { $_.ProviderName } | ForEach-Object { "$($_.DeviceID)=$($_.ProviderName)" })
    New-Item -ItemType Directory -Force -Path (Join-Path $Root "run") | Out-Null
    Set-Content -Path (Join-Path $Root "run\drives.txt") -Value $drives -Encoding ASCII
    if ($drives) { Write-Host "Network drives it will map for itself: $($drives -join ', ')" }
    else { Write-Host "No network drives mapped now: agent.yaml and ComfyUI must use \\server\share paths." -ForegroundColor Yellow }
}

function Register-OurTask {
    $user = "$env:USERDOMAIN\$env:USERNAME"
    $cred = Get-Credential -UserName $user -Message ("Your Windows password. The agent then runs whether or not you're " +
        "logged in; Windows keeps the password with the task. Run -Always again after you change it.")
    if (-not $cred) { throw "No password given: nothing changed." }
    $action = New-ScheduledTaskAction -Execute $powershell -Argument $psArgs -WorkingDirectory $Root
    # Every 5 minutes, for good: a run while the agent runs ends at once (one runner per agent.yaml).
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 9999)
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -MultipleInstances IgnoreNew `
        -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -DontStopOnIdleEnd
    try {
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
            -User $user -Password $cred.GetNetworkCredential().Password -RunLevel Limited -Force `
            -Description "ComfyFarm agent ($runner): kept running whether or not $user is logged in" | Out-Null
    } catch {
        Write-Host @"

Windows refused the task: $($_.Exception.Message)

Running while you're logged out needs the "Log on as a batch job" right for $user on this
machine (or a wrong password was typed). IT can grant that right, or install the agent as a
service. Nothing changed: the agent keeps starting at logon.
"@ -ForegroundColor Yellow
        exit 1
    }
    Write-Host "The agent now runs whether or not you're logged in (scheduled task '$taskName', every 5 minutes)."
}

$task = Get-OurTask

if ($Stop -or $Remove) {
    if ($task) {
        Disable-ScheduledTask -TaskName $taskName | Out-Null      # not started again in 5 minutes
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    }
    Stop-Agent
}
if ($Remove) {
    if ($task) { Unregister-ScheduledTask -TaskName $taskName -Confirm:$false; Write-Host "Removed the scheduled task '$taskName'." }
    if (Test-Path $link) { Remove-Item $link; Write-Host "Removed ${link}." }
    Write-Host "The agent no longer starts by itself."
    exit 0
}
if ($Stop -and -not $StartNow) {
    if ($task) { Write-Host "Stopped. The task is paused until: install_startup.ps1 -StartNow" }
    exit 0
}

if ($Always) {
    Save-Drives
    Register-OurTask
    if (-not $task) {
        # Switching from at logon: the agent running in your session stops, and the task
        # runs it from now on (one agent, not two). ComfyUI keeps running.
        Stop-Agent
    }
    $task = Get-OurTask
    if (Test-Path $link) { Remove-Item $link; Write-Host "Removed the logon shortcut: the task covers logons too." }
    if (-not $StartNow) { Write-Host "The task starts it within 5 minutes (or now with -StartNow)." }
} elseif ($AtLogon -or -not $task) {
    if ($task) {
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        Stop-Agent
        Write-Host "Removed the scheduled task '$taskName'."
        $task = $null
    }
    Set-Shortcut
}

if ($StartNow) {
    if ($task) {
        Enable-ScheduledTask -TaskName $taskName | Out-Null
        Start-ScheduledTask -TaskName $taskName
    } else {
        Start-Process -FilePath $powershell -ArgumentList $psArgs -WindowStyle Hidden
    }
    Write-Host "Started. Its log: $Root\logs\agent-<name>.log (and logs\agent-console.log)"
}
