<#
.SYNOPSIS
  First-time install of the ComfyUI farm gateway (safe to re-run).

.DESCRIPTION
  Creates .venv, installs requirements, creates config.yaml from the example if
  missing, and runs the preflight check. Run from anywhere:

    powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
#>
param(
    [string]$Python = ""   # optional: full path to python.exe (3.10+)
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Find-Python {
    # Windows PowerShell 5.1 turns redirected stderr from exes into errors; don't stop on them here.
    $ErrorActionPreference = "Continue"
    if ($Python) { return @($Python) }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($v in "3.13", "3.12", "3.11", "3.10") {
            & py "-$v" -c "import sys" 2>$null
            if ($LASTEXITCODE -eq 0) { return @("py", "-$v") }
        }
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        $ok = & python -c "import sys; print(sys.version_info >= (3, 10))" 2>$null
        if ($ok -eq "True") { return @("python") }
    }
    throw "Python 3.10+ not found. Install it from python.org (tick 'Add python.exe to PATH') and re-run."
}

$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    $py = Find-Python
    Write-Host "Creating virtual environment with $($py -join ' ')..."
    if ($py.Count -gt 1) { & $py[0] $py[1] -m venv .venv } else { & $py[0] -m venv .venv }
    if ($LASTEXITCODE -ne 0) { throw "venv creation failed" }
}

Write-Host "Installing requirements..."
& $venvPy -m pip install --upgrade pip --quiet
& $venvPy -m pip install -r requirements.txt --quiet
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

if (-not (Test-Path "config.yaml")) {
    Copy-Item "config.example.yaml" "config.yaml"
    Write-Host "Created config.yaml from config.example.yaml - review it before going live." -ForegroundColor Yellow
}
New-Item -ItemType Directory -Force -Path data, logs | Out-Null

# Movie inputs need ffmpeg + ffprobe (EXR inputs use oiiotool, installed above with OpenImageIO).
if (-not (Get-Command ffprobe -ErrorAction SilentlyContinue)) {
    Write-Host "ffmpeg/ffprobe not found on PATH - movie inputs (.mov/.mp4) will be refused." -ForegroundColor Yellow
    Write-Host "  Install:  winget install Gyan.FFmpeg   (then open a new terminal / restart the service)" -ForegroundColor Yellow
}

Write-Host "`nPreflight check (ComfyUI must be running for the worker checks):" -ForegroundColor Cyan
& $venvPy -m comfyfarm check

Write-Host @"

Next:
  Try it now:        scripts\run_gateway.bat   then open http://localhost:8000
  Run as services:   scripts\install_services.ps1   (as Administrator)
  Open the port:     scripts\open_firewall.ps1      (as Administrator)
"@
