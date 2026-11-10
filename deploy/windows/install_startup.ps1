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
  Windows may accept the task and only refuse to run it, so -Always starts it once (the
  agent then runs from it) and reads its result: when Windows won't run it, the task is
  removed, the logon shortcut put back, and this says why. IT can grant the right, or
  install the agent as a service (install_services.ps1). The task runs in a background session with
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
}

function Test-OurTask {
    # Registering doesn't tell whether Windows will run the task: it checks the "Log on as a
    # batch job" right only when it starts it. So start it once and look. "" when it runs,
    # else the result code.
    try {
        Enable-ScheduledTask -TaskName $taskName | Out-Null
        Start-ScheduledTask -TaskName $taskName
    } catch { return $_.Exception.Message }
    for ($i = 0; $i -lt 15; $i++) {
        Start-Sleep -Seconds 2
        $state = (Get-ScheduledTask -TaskName $taskName).State
        $code = [uint32](Get-ScheduledTaskInfo -TaskName $taskName).LastTaskResult
        if ($state -eq "Running" -or $code -eq 0) { return "" }
        if ($code -ne 0x41303 -and $code -ne 0x41301) { return ("0x{0:X}" -f $code) }   # 0x41303: not run yet
    }
    return "it hadn't started after 30 seconds"
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
    $switching = -not $task
    Register-OurTask
    if ($switching) {
        # From at logon: the agent running in your session stops, and the task runs it from
        # now on (one agent, not two). ComfyUI keeps running.
        Stop-Agent
    }
    $failed = Test-OurTask
    if ($failed) {
        $known = @{ "0x80070569" = "your account lacks the 'Log on as a batch job' right on this machine";
                    "0x8007052E" = "Windows didn't accept the password";
                    "0x800710E0" = "a policy on this machine refuses it" }
        $reason = if ($known[$failed]) { "${failed}: $($known[$failed])" } else { $failed }
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        Write-Host @"

Windows accepted the task but wouldn't run it ($reason).
Running while you're logged out needs the "Log on as a batch job" right for
$env:USERDOMAIN\$env:USERNAME on this machine: IT can grant it, or install the agent as a
service. The task is removed; the agent starts at logon, as before.
"@ -ForegroundColor Yellow
        Set-Shortcut
        if ($switching) { Start-Process -FilePath $powershell -ArgumentList $psArgs -WindowStyle Hidden }   # as it was
        exit 1
    }
    $task = Get-OurTask
    if (Test-Path $link) { Remove-Item $link; Write-Host "Removed the logon shortcut: the task covers logons too." }
    Write-Host "Running: the agent now runs whether or not you're logged in (scheduled task '$taskName', every 5 minutes)."
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
