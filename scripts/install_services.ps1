<#
.SYNOPSIS
  Install the gateway (and optionally ComfyUI) as Windows services with NSSM, so they
  start at boot, keep running when nobody is logged in, and restart if they crash.

.EXAMPLE
  # Gateway only (ComfyUI keeps running in your own session):
  powershell -ExecutionPolicy Bypass -File scripts\install_services.ps1 -User "RODEO\svc_comfy"

.EXAMPLE
  # Gateway + ComfyUI portable:
  powershell -ExecutionPolicy Bypass -File scripts\install_services.ps1 `
      -ComfyUIDir "C:\ComfyUI_windows_portable" -User "RODEO\svc_comfy"

.NOTES
  - Needs NSSM (https://nssm.cc): put nssm.exe on PATH or pass -Nssm C:\tools\nssm.exe
  - -User should be a domain account that can read the plates and write the renders
    folder on the show share. Services running as LocalSystem usually can't.
  - ComfyUI is started WITHOUT --listen, so only this machine (the gateway) can reach it.
#>
#Requires -RunAsAdministrator
param(
    [string]$Nssm = "nssm.exe",
    [string]$ServiceName = "ComfyFarm",
    [string]$ComfyUIDir = "",
    [string]$ComfyServiceName = "ComfyUI",
    [string]$ComfyArgs = "--windows-standalone-build --disable-auto-launch",
    [string]$User = ""
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot

$nssmExe = (Get-Command $Nssm -ErrorAction SilentlyContinue).Source
if (-not $nssmExe) { throw "nssm.exe not found. Download it from https://nssm.cc and pass -Nssm <path>." }
$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) { throw "Run scripts\setup.ps1 first." }
$logDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$password = $null
if ($User) {
    $cred = Get-Credential -UserName $User -Message "Password for the service account (needs access to the show share)"
    $password = $cred.GetNetworkCredential().Password
}

function Set-Service-Nssm([string]$Name, [string]$Exe, [string]$Arguments, [string]$Dir, [string]$Log) {
    $exists = Get-Service -Name $Name -ErrorAction SilentlyContinue
    if ($exists) {
        & $nssmExe stop $Name | Out-Null
        & $nssmExe set $Name Application $Exe | Out-Null
    } else {
        & $nssmExe install $Name $Exe | Out-Null
    }
    & $nssmExe set $Name AppParameters $Arguments | Out-Null
    & $nssmExe set $Name AppDirectory $Dir | Out-Null
    & $nssmExe set $Name AppStdout $Log | Out-Null
    & $nssmExe set $Name AppStderr $Log | Out-Null
    & $nssmExe set $Name AppRotateFiles 1 | Out-Null
    & $nssmExe set $Name AppRotateOnline 1 | Out-Null
    & $nssmExe set $Name AppRotateBytes 10485760 | Out-Null
    & $nssmExe set $Name AppExit Default Restart | Out-Null
    & $nssmExe set $Name AppRestartDelay 5000 | Out-Null
    & $nssmExe set $Name Start SERVICE_AUTO_START | Out-Null
    if ($User) { & $nssmExe set $Name ObjectName $User $password | Out-Null }
    Write-Host "Service $Name configured."
}

if ($ComfyUIDir) {
    $embedded = Join-Path $ComfyUIDir "python_embeded\python.exe"
    if (-not (Test-Path $embedded)) { throw "$embedded not found - is -ComfyUIDir the portable install folder?" }
    Set-Service-Nssm $ComfyServiceName $embedded "-s ComfyUI\main.py $ComfyArgs" $ComfyUIDir (Join-Path $logDir "comfyui-service.log")
}

Set-Service-Nssm $ServiceName $venvPy "-m comfyfarm serve" $Root (Join-Path $logDir "gateway-service.log")
if ($ComfyUIDir) { & $nssmExe set $ServiceName DependOnService $ComfyServiceName | Out-Null }

if ($ComfyUIDir) { Start-Service $ComfyServiceName }
Start-Service $ServiceName
$names = @($ServiceName)
if ($ComfyUIDir) { $names += $ComfyServiceName }
Get-Service -Name $names | Format-Table Name, Status, StartType
Write-Host "Dashboard: http://$($env:COMPUTERNAME):8000  (open the firewall with scripts\open_firewall.ps1)"
