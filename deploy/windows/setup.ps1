<#
.SYNOPSIS
  First-time install on a Windows machine (safe to re-run): a GPU machine's agent, or the
  whole farm on one machine (standalone). No admin rights needed.

.DESCRIPTION
  Creates .venv, installs requirements, creates agent.yaml (-Agent) or config.yaml from
  the example if missing, links the farm's own ComfyUI nodes (comfyui_nodes\) into
  ComfyUI, and runs the check. Run from anywhere:

    powershell -ExecutionPolicy Bypass -File deploy\windows\setup.ps1 -Agent -ComfyUIDir "C:\ComfyUI_windows_portable"
    powershell -ExecutionPolicy Bypass -File deploy\windows\setup.ps1 -ComfyUIDir "C:\ComfyUI_windows_portable"
#>
param(
    [switch]$Agent,            # a GPU machine for a manager elsewhere (agent.yaml), not standalone (config.yaml)
    [string]$Python = "",      # optional: full path to python.exe (3.10+)
    [string]$ComfyUIDir = ""   # ComfyUI's folder (portable install, the one holding custom_nodes, or custom_nodes)
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)   # the repo
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

$cfgFile = if ($Agent) { "agent.yaml" } else { "config.yaml" }
if (-not (Test-Path $cfgFile)) {
    Copy-Item ($cfgFile -replace '\.yaml$', '.example.yaml') $cfgFile
    Write-Host "Created $cfgFile from its example - edit it before going live." -ForegroundColor Yellow
}
New-Item -ItemType Directory -Force -Path data, logs | Out-Null

# The farm's own ComfyUI nodes (ComfyFarm Save Image, ...) live in this repo. A junction
# in ComfyUI's custom_nodes points at them, so git pull updates them too (restart ComfyUI
# to load a change). Junctions need no admin rights.
$nodes = Join-Path $Root "comfyui_nodes\comfyfarm_nodes"
if ($ComfyUIDir) {
    # The ComfyUI folder, a portable install (ComfyUI\ inside it), or custom_nodes itself.
    $ComfyUIDir = $ComfyUIDir.TrimEnd([char]'\', [char]'/')
    $custom = @((Join-Path $ComfyUIDir "custom_nodes"), (Join-Path $ComfyUIDir "ComfyUI\custom_nodes")) |
        Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $custom -and (Split-Path $ComfyUIDir -Leaf) -eq "custom_nodes" -and (Test-Path $ComfyUIDir)) {
        $custom = $ComfyUIDir
    }
    if (-not $custom) { throw "No custom_nodes folder in $ComfyUIDir or $ComfyUIDir\ComfyUI (pass ComfyUI's folder, e.g. D:\ComfyUI)" }
    $link = Join-Path $custom "comfyfarm_nodes"
    $item = Get-Item $link -ErrorAction SilentlyContinue
    if ($item -and $item.LinkType -eq "Junction" -and ($item.Target -replace '\\$', '') -eq $nodes) {
        Write-Host "ComfyUI nodes already linked: $link"
    } else {
        if ($item -and $item.LinkType -eq "Junction") { $item.Delete() }
        elseif ($item) { throw "$link exists and isn't a link to this repo; move it away and re-run" }
        New-Item -ItemType Junction -Path $link -Target $nodes | Out-Null
        Write-Host "Linked the farm's ComfyUI nodes: $link -> $nodes (restart ComfyUI to load them)" -ForegroundColor Yellow
    }
} else {
    Write-Host "Pass -ComfyUIDir <ComfyUI folder> to link the farm's ComfyUI nodes (needed by chord_pbr)." -ForegroundColor Yellow
}

# EXR inputs use oiiotool (OpenImageIO) and movie inputs use ffmpeg (imageio-ffmpeg);
# both were installed into .venv above with the requirements. The check below lists them.

if ($Agent) {
    Write-Host "`nChecking this worker (ComfyUI must be running, or set comfyui_launch in agent.yaml):" -ForegroundColor Cyan
    & $venvPy -m comfyfarm agent --check
    Write-Host @"

Next:
  Edit agent.yaml (name, manager, token, paths), then check again:
                     .venv\Scripts\python.exe -m comfyfarm agent --check
  Try it now:        .venv\Scripts\python.exe -m comfyfarm agent     (Ctrl+C to stop)
  Start at logon:    deploy\windows\install_startup.ps1 -StartNow  (no admin; restarts it if it exits)
"@
} else {
    Write-Host "`nPreflight check (ComfyUI must be running for the worker checks):" -ForegroundColor Cyan
    & $venvPy -m comfyfarm check
    Write-Host @"

Next:
  Try it now:        deploy\windows\run_gateway.bat   then open http://localhost:8000
  Run as services:   deploy\windows\install_services.ps1   (as Administrator)
  Open the port:     deploy\windows\open_firewall.ps1      (as Administrator)
"@
}
