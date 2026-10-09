<#
.SYNOPSIS
  Pull the latest version from GitHub, update dependencies, check, and restart the service.

  powershell -ExecutionPolicy Bypass -File scripts\update.ps1
#>
param([string]$ServiceName = "ComfyFarm")
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$before = (git rev-parse HEAD).Trim()
git pull --ff-only
if ($LASTEXITCODE -ne 0) { throw "git pull failed (local edits? config.yaml is ignored, so it's safe)" }
$after = (git rev-parse HEAD).Trim()
if ($before -eq $after) { Write-Host "Already up to date." } else { git log --oneline "$before..$after" }

& .venv\Scripts\python.exe -m pip install -r requirements.txt --quiet
& .venv\Scripts\python.exe -m comfyfarm check

if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
    try {
        Restart-Service $ServiceName
        Write-Host "Restarted $ServiceName. Queued and running jobs carry on: state lives in data\comfyfarm.db."
    } catch {
        Write-Warning "Couldn't restart $ServiceName (run this as Administrator): $_"
    }
}
