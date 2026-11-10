<#
.SYNOPSIS
  A farm agent on a Windows GPU machine, from nothing, without admin rights. Run it in the
  folder that should hold everything (this machine's own disk):

    mkdir D:\comfyfarm; cd D:\comfyfarm
    git clone https://github.com/PeterDominik/comfyui-farm.git
    powershell -ExecutionPolicy Bypass -File comfyui-farm\deploy\windows\install_agent.ps1 -Always

.DESCRIPTION
  It puts next to each other in that folder:
    python\         Python from python.org, for you only (unless -Python names one, or the
                    machine already has the version deploy\comfyui\environment.yaml pins)
    comfyui\        ComfyUI at the farm's commit
    comfyui-env\    ComfyUI's Python environment, with the farm's torch
    comfyui-farm\   this repo (cloned if it isn't there yet), its .venv and agent.yaml
  then brings ComfyUI to the farm's (node packs, extra_model_paths.yaml, package pins:
  comfyfarm comfyui sync), starts the agent (it starts ComfyUI) and keeps it running: at
  logon, or with -Always whether or not you're logged in (see install_startup.ps1).

  Safe to re-run: what's there is kept, what's missing is added. A running agent is left
  running (ComfyUI is only brought to the farm's when the agent isn't running).
  Everything it prints also goes to install_agent.log in the folder.

  Needs Git for Windows (git-scm.com; "Only for me" needs no admin) and the NVIDIA driver.
#>
param(
    [string]$Dir = "",                                  # install there instead of the current folder
    [string]$Name = "",                                 # the agent's name (default: <computer>-<gpu>, e.g. wrk59467w-5090)
    [int]$Port = 8188,                                  # ComfyUI's port
    [string]$Manager = "B:/comfy_farm/manager.json",    # the manager's manager.json (/rdo/bank is B:) or URL
    [string]$Shows = "//rodeofx.com/smb/shows",         # how this machine reaches /rdo/shows
    [string]$Branch = "main",                           # the branch to clone when comfyui-farm\ isn't there
    [string]$TokenFile = "",                            # a file with the manager's agents.token (default: asks)
    [string]$Python = "",                               # a python.exe of the pinned version to build the environments on
    [switch]$Always,                                    # keep it running whether or not you're logged in
    [switch]$NoStart                                    # install and check, but don't start it
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"               # Invoke-WebRequest is far slower with its progress bar
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }

$RepoUrl = if ($env:COMFYFARM_REPO_URL) { $env:COMFYFARM_REPO_URL } else { "https://github.com/PeterDominik/comfyui-farm.git" }
$PyPI = "https://pypi.org/simple"
$MinFreeGB = 40
# python.org's last Windows installer of each Python version the farm may pin.
$PythonBuilds = @{ "3.10" = "3.10.11"; "3.11" = "3.11.9"; "3.12" = "3.12.10" }

if (-not $Dir) { $Dir = (Get-Location).Path }
$Dir = (Resolve-Path $Dir).Path
if ($Dir.Length -gt 3) { $Dir = $Dir.TrimEnd('\') }
Set-Location $Dir
Start-Transcript -Path (Join-Path $Dir "install_agent.log") -Append | Out-Null

function Step([string]$Text) { Write-Host "`n==> $Text" -ForegroundColor Cyan }
function Note([string]$Text) { Write-Host "    $Text" }
function Stop-Install([string]$Text) {
    Write-Host "`nSTOPPED: $Text" -ForegroundColor Red
    Stop-Transcript | Out-Null
    exit 1
}
function Run {
    # Runs a program, showing its output (so the transcript has it too); a failure stops the
    # install unless -AllowFail. The exit code is left in $script:Code.
    param([string]$Exe, [string[]]$Arguments, [switch]$AllowFail)
    $old = $ErrorActionPreference
    $ErrorActionPreference = "Continue"                # Windows PowerShell turns a program's stderr into errors
    & $Exe @Arguments 2>&1 | ForEach-Object { Write-Host "$_" }
    $script:Code = $LASTEXITCODE
    $ErrorActionPreference = $old
    if ($script:Code -ne 0 -and -not $AllowFail) { Stop-Install "$Exe $($Arguments -join ' ') failed (exit $script:Code)" }
}
function Get-Output {
    # A program's standard output as text, or "" if it fails.
    param([string]$Exe, [string[]]$Arguments)
    $old = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $out = & $Exe @Arguments 2>$null
    $ok = $LASTEXITCODE -eq 0
    $ErrorActionPreference = $old
    if ($ok) { return ((@($out) | ForEach-Object { "$_" }) -join "`n").Trim() } else { return "" }   # lines kept apart
}
function Set-OwnerOnly([string]$Path) {
    # Readable by you only: agent.yaml holds the manager's token.
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    Run icacls @($Path, "/inheritance:r", "/grant:r", "*${sid}:(F)", "*S-1-5-18:(F)") | Out-Null
}

# The pipeline's Python settings must not reach the farm's environments; pip's cache stays
# on this disk.
foreach ($v in "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
$env:PYTHONNOUSERSITE = "1"
$env:PYTHONUTF8 = "1"
$env:PIP_CACHE_DIR = Join-Path $Dir "pip-cache"

$Farm = Join-Path $Dir "comfyui-farm"
$Comfy = Join-Path $Dir "comfyui"
$ComfyEnv = Join-Path $Dir "comfyui-env"
$Cpy = Join-Path $ComfyEnv "Scripts\python.exe"
$Venv = Join-Path $Farm ".venv\Scripts\python.exe"
$AgentYaml = Join-Path $Farm "agent.yaml"
Write-Host "===== $(Get-Date -Format 's') install_agent.ps1 in $Dir"

# ---------------------------------------------------------------- the machine
Step "Checking this machine"
if (-not [Environment]::Is64BitOperatingSystem) { Stop-Install "this script is for 64-bit Windows" }
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Stop-Install "git isn't installed: install Git for Windows (git-scm.com, 'Only for me' needs no admin), then run this again"
}
if (-not (Get-Command nvidia-smi -ErrorAction SilentlyContinue)) { Stop-Install "nvidia-smi isn't there: no NVIDIA driver on this machine?" }
$gpus = @(((Get-Output nvidia-smi @("--query-gpu=name,driver_version", "--format=csv,noheader")) -split "`r?`n") | Where-Object { $_ })
if (-not $gpus) { Stop-Install "nvidia-smi lists no GPU" }
$gpu, $driver = $gpus[0] -split ",\s*", 2
Note "GPU: $gpu, driver $driver"
if ($gpus.Count -gt 1) { Note "$($gpus.Count) GPUs: this installs one agent, for the first one" }
try { $qualifier = Split-Path -Qualifier $Dir } catch { $qualifier = "" }
$disk = if ($qualifier) { Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='$qualifier'" } else { $null }
if (-not $disk -or $disk.DriveType -ne 3) { Stop-Install "$Dir isn't on this machine's own disk: install on a local drive (D:\...)" }
$freeGB = [math]::Floor($disk.FreeSpace / 1GB)
Note "${Dir}: $freeGB GB free"
if ($freeGB -lt $MinFreeGB) { Stop-Install "$Dir has $freeGB GB free; ComfyUI, its Python and the scratch frames need about $MinFreeGB GB" }
$showsWin = $Shows -replace '/', '\'
if (-not (Test-Path $showsWin)) { Stop-Install "$showsWin isn't reachable from this machine (show storage; -Shows to change it)" }
if ($Manager -notmatch '^https?://' -and -not (Test-Path ($Manager -replace '/', '\'))) {
    Stop-Install "can't read ${Manager}: is the manager running, and is /rdo/bank mapped as B:? (or pass -Manager http://<host>:<port>)"
}
if (-not $Name) {
    $short = (($gpu.ToLower() -replace 'nvidia|geforce|rtx|workstation edition|blackwell|max-q', ' ').Trim()) -replace '\s+', '-'
    if (-not $short) { $short = "gpu" }
    $Name = "$($env:COMPUTERNAME.ToLower())-$short"
}
Note "agent name: $Name"

# ---------------------------------------------------------------- the farm's checkout
if (-not (Test-Path (Join-Path $Farm ".git"))) {
    if (Test-Path $Farm) { Stop-Install "$Farm exists but isn't a git checkout: move it away first" }
    Step "Cloning the farm ($Branch) into $Farm"
    Run git @("clone", "--branch", $Branch, $RepoUrl, $Farm)
} else {
    Step "The farm's checkout: $Farm at $(Get-Output git @('-C', $Farm, 'rev-parse', '--short', 'HEAD')) ($(Get-Output git @('-C', $Farm, 'rev-parse', '--abbrev-ref', 'HEAD')))"
}
$specFile = Join-Path $Farm "deploy\comfyui\environment.yaml"
$m = Select-String -Path $specFile -Pattern "^python:\s*'?(\d+\.\d+)" | Select-Object -First 1
$pyVer = if ($m) { $m.Matches[0].Groups[1].Value } else { "3.12" }

# ---------------------------------------------------------------- Python
function Test-PythonVersion([string]$Exe) {
    return (Get-Output $Exe @("-c", "import sys; print('%d.%d' % sys.version_info[:2])")) -eq $pyVer
}
$ownPy = Join-Path $Dir "python\python.exe"
if ($Python) {
    if (-not (Test-PythonVersion $Python)) { Stop-Install "$Python isn't Python $pyVer, the farm's" }
    $py = $Python
} elseif (Test-Path $ownPy) {
    $py = $ownPy
} else {
    $found = if (Get-Command py -ErrorAction SilentlyContinue) { Get-Output py @("-$pyVer", "-c", "import sys; print(sys.executable)") } else { "" }
    if ($found) {
        $py = $found
        Step "Python ${pyVer}: this machine's ($py)"
    } else {
        $build = $PythonBuilds[$pyVer]
        if (-not $build) { Stop-Install "no python.org installer known for Python ${pyVer}: pass -Python <a python.exe of that version>" }
        Step "Installing Python $build from python.org into $Dir\python (for you only, no admin)"
        $inst = Join-Path $Dir "python-$build-amd64.exe"
        Invoke-WebRequest -UseBasicParsing -Uri "https://www.python.org/ftp/python/$build/python-$build-amd64.exe" -OutFile $inst
        $p = Start-Process -FilePath $inst -Wait -PassThru -ArgumentList @(
            "/quiet", "InstallAllUsers=0", "TargetDir=`"$Dir\python`"", "Include_launcher=0", "Include_test=0",
            "Include_doc=0", "AssociateFiles=0", "Shortcuts=0", "PrependPath=0")
        Remove-Item $inst -ErrorAction SilentlyContinue
        if ($p.ExitCode -ne 0 -or -not (Test-Path $ownPy)) {
            Stop-Install ("the Python installer ended with $($p.ExitCode) and $ownPy isn't there. If Python $build is already " +
                          "installed for you somewhere else, pass -Python <its python.exe>")
        }
        $py = $ownPy
    }
}
Note "Python: $py"

# ---------------------------------------------------------------- agent.yaml
$backupDir = Join-Path $env:USERPROFILE ".config\comfyfarm"
$backup = Join-Path $backupDir "agent-$Name.yaml"
$example = Join-Path $Farm "agent.example.yaml"
$kept = (Test-Path $AgentYaml) -and ((Get-FileHash $AgentYaml).Hash -ne (Get-FileHash $example).Hash)
if ($kept) {
    Step "agent.yaml is there: kept as it is"
    $m = Select-String -Path $AgentYaml -Pattern '^comfyui:.*:(\d+)' | Select-Object -First 1
    if ($m) { $Port = [int]$m.Matches[0].Groups[1].Value }
    $m = Select-String -Path $AgentYaml -Pattern "^name:\s*'?([^'#\s]+)" | Select-Object -First 1
    if ($m) { $Name = $m.Matches[0].Groups[1].Value; $backup = Join-Path $backupDir "agent-$Name.yaml" }
    Note "agent name: $Name, ComfyUI on port $Port"
} else {
    Step "Writing agent.yaml"
    if ($TokenFile) {
        $token = (Get-Content $TokenFile -Raw).Trim()
    } elseif ($env:COMFYFARM_TOKEN) {
        $token = $env:COMFYFARM_TOKEN
    } elseif ((Test-Path $backup) -and (Select-String -Path $backup -Pattern '^token:' -Quiet)) {
        $token = (Select-String -Path $backup -Pattern "^token:\s*'?([^']*)'?\s*$" | Select-Object -First 1).Matches[0].Groups[1].Value
        Note "token from $backup"
    } else {
        $secure = Read-Host "    The manager's agents.token (config.yaml on the manager)" -AsSecureString
        $token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
    }
    if (-not $token) { Note "warning: empty token; a manager with agents.token set will refuse this agent" }
    $busy = $false
    try { $c = New-Object Net.Sockets.TcpClient; $c.Connect("127.0.0.1", $Port); $busy = $true; $c.Close() } catch { }
    if ($busy) { Stop-Install "port $Port is in use on this machine (another ComfyUI?): stop it, or pass -Port <another>" }
    $tokenYaml = $token -replace "'", "''"              # YAML single quotes: ' is written ''
    $cpyFwd = $Cpy -replace '\\', '/'
    $comfyFwd = $Comfy -replace '\\', '/'
    $yaml = @"
# Written by deploy\windows\install_agent.ps1 on $(Get-Date -Format 'yyyy-MM-dd'). See agent.example.yaml for the rest.
name: $Name
manager: '$Manager'
token: '$tokenYaml'
comfyui: http://127.0.0.1:$Port
max_inflight: 2
comfyui_launch:
  command: ['$cpyFwd', main.py, --port, '$Port']
  cwd: '$comfyFwd'
paths:
  mappings:
    - posix: /rdo/shows
      windows: $Shows
"@
    [IO.File]::WriteAllText($AgentYaml, ($yaml -replace "`r?`n", "`r`n"), (New-Object Text.UTF8Encoding $false))
}
Set-OwnerOnly $AgentYaml
New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
Copy-Item $AgentYaml $backup -Force
Set-OwnerOnly $backup
Note "a copy is kept in $backup"

# ---------------------------------------------------------------- the agent's Python
Step "The agent's Python (.venv in the checkout)"
& (Join-Path $Farm "deploy\windows\setup.ps1") -Agent -Python $py -NoCheck
Set-Location $Dir
if (-not (Test-Path $Venv)) { Stop-Install "setup.ps1 didn't make $Venv (see above)" }

# What the farm's ComfyUI is: deploy\comfyui\environment.yaml, read with the farm's code.
$code = @'
import json
from comfyfarm.comfyenv import machine as M, spec as S
s = S.load() or {}
c, pk = s.get("comfyui") or {}, s.get("packages") or {}
pins = [f"{n}=={pk[n]}" for n in M.FULL_VERSION if isinstance(pk.get(n), str)]
cuda = S.cuda_of(pk.get("torch"))
libs = {lib["path"]: lib.get("windows") for lib in (s.get("libraries") or S.LIBRARIES)}
shared = []
for sec in S.farm_model_paths():
    win = libs.get(sec["library"])
    for p in sec["given"].get("custom_nodes", []):
        if win:
            shared.append(win.rstrip("\\") + "\\" + p.strip("/").replace("/", "\\"))
print(json.dumps({"commit": c.get("commit") or "", "url": c.get("url") or "https://github.com/Comfy-Org/ComfyUI.git",
                  "version": c.get("version") or "", "driver_min": int(s.get("driver_min") or 0), "torch": pins,
                  "torch_index": f"https://download.pytorch.org/whl/cu{cuda[0]}{cuda[1]}" if cuda else "",
                  "libraries": [w for w in libs.values() if w], "shared_nodes": shared}))
'@
Push-Location $Farm
$ErrorActionPreference = "Continue"
$json = $code | & $Venv -
$ErrorActionPreference = "Stop"
Pop-Location
try { $spec = ("$json" | ConvertFrom-Json) } catch { Stop-Install "couldn't read $specFile with the farm's Python: $json" }
if (-not $spec.commit -or -not $spec.torch) { Stop-Install "$specFile has no ComfyUI commit or torch" }
$major = [int](($driver -split '\.')[0])
if ($major -lt $spec.driver_min) {
    Stop-Install "NVIDIA driver ${driver}: the farm's torch ($($spec.torch -join ' ')) needs $($spec.driver_min) or newer. Updating it needs IT"
}
foreach ($lib in $spec.libraries) {
    if (-not (Test-Path $lib)) { Stop-Install "the model library $lib isn't reachable (is /rdo/bank mapped as B:?)" }
}

# ---------------------------------------------------------------- ComfyUI
if (-not (Test-Path (Join-Path $Comfy ".git"))) {
    if (Test-Path $Comfy) { Stop-Install "$Comfy exists but isn't a git checkout: move it away first" }
    Step "Cloning ComfyUI $($spec.version) ($($spec.commit.Substring(0, 7))) into $Comfy"
    Run git @("clone", "--quiet", $spec.url, $Comfy)
    Run git @("-C", $Comfy, "checkout", "--quiet", "--detach", $spec.commit)
} else {
    $at = Get-Output git @("-C", $Comfy, "rev-parse", "--short", "HEAD")
    Step "ComfyUI: $Comfy at $(if ($at) { $at } else { 'an unknown commit' }) (sync brings it to $($spec.commit.Substring(0, 7)))"
}

if (-not (Test-Path $Cpy)) {
    Step "ComfyUI's Python environment ($ComfyEnv)"
    Run $py @("-m", "venv", $ComfyEnv)
}
$wantTorch = ($spec.torch[0] -split '==')[1]
if ((Get-Output $Cpy @("-c", "import importlib.metadata as m; print(m.version('torch'))")) -ne $wantTorch) {
    Step "torch for ComfyUI: $($spec.torch -join ' ') (a few GB)"
    $pipArgs = @("-m", "pip", "install") + $spec.torch
    if ($spec.torch_index) { $pipArgs += @("--index-url", $spec.torch_index) }
    Run $Cpy $pipArgs
}
Run $Cpy @("-c", "import torch; assert torch.cuda.is_available(), 'CUDA not available'; print('    torch', torch.__version__, 'on', torch.cuda.get_device_name(0))") -AllowFail
if ($script:Code -ne 0) { Stop-Install "torch can't use the GPU (see above)" }

Step "ComfyUI's requirements, and those of the farm library's shared node packs (torch held)"
$held = Join-Path $Farm "run\torch-held.txt"
New-Item -ItemType Directory -Force -Path (Split-Path $held) | Out-Null
Set-Content -Path $held -Value $spec.torch -Encoding ASCII
$reqs = @("-r", (Join-Path $Comfy "requirements.txt"))
foreach ($d in $spec.shared_nodes) {
    foreach ($pack in @(Get-ChildItem -Path $d -Directory -ErrorAction SilentlyContinue)) {
        $r = Join-Path $pack.FullName "requirements.txt"
        if (Test-Path $r) { $reqs += @("-r", $r) }
    }
}
Run $Cpy (@("-m", "pip", "install", "--prefer-binary", "--index-url", $PyPI, "-c", $held) + $reqs)

# The farm's own nodes: a junction in ComfyUI's custom_nodes (no admin needed).
$nodes = Join-Path $Farm "comfyui_nodes\comfyfarm_nodes"
$link = Join-Path $Comfy "custom_nodes\comfyfarm_nodes"
$item = Get-Item $link -ErrorAction SilentlyContinue
if ($item -and $item.LinkType -eq "Junction") { $item.Delete() }
elseif ($item) { Stop-Install "$link exists and isn't a link to the farm's checkout: move it away and re-run" }
New-Item -ItemType Junction -Path $link -Target $nodes | Out-Null
Note "the farm's nodes: $link -> $nodes"

# ---------------------------------------------------------------- the farm's ComfyUI
$agentPid = Join-Path $Farm "logs\agent.pid"
$running = (Test-Path $agentPid) -and (Get-Process -Id ([int](Get-Content $agentPid -TotalCount 1)) -ErrorAction SilentlyContinue)
$sync = @("-m", "comfyfarm", "comfyui", "sync", "--config", $AgentYaml, "--apply", "--no-manager")
if ($running) {
    Step "The agent is running: ComfyUI is left as it is"
    Note "To bring it to the farm's: drain the machine on the dashboard, then"
    Note "$Venv -m comfyfarm comfyui sync --config $AgentYaml --apply"
} else {
    Step "Bringing ComfyUI to the farm's (node packs, extra_model_paths.yaml, package pins)"
    # The agent isn't running, so it holds no jobs and ComfyUI isn't in use: no need to ask
    # the manager. Twice: the second pass pins what the first one's requirements installed.
    Run $Venv $sync -AllowFail
    Run $Venv $sync -AllowFail
}
Step "How this machine's ComfyUI compares with the farm's"
Run $Venv @("-m", "comfyfarm", "comfyui", "verify", "--config", $AgentYaml) -AllowFail

# ---------------------------------------------------------------- start
$startup = Join-Path $Farm "deploy\windows\install_startup.ps1"
if ($NoStart) {
    Step "Installed. Not started (-NoStart). To start it and keep it running:"
    Note "powershell -ExecutionPolicy Bypass -File $startup -Always -StartNow"
    Stop-Transcript | Out-Null
    exit 0
}
Step "Starting the agent (it starts ComfyUI)"
$global:LASTEXITCODE = 0
if ($Always) {
    & $startup -Always -StartNow
    if ($LASTEXITCODE -ne 0) {
        Note "Starting it at logon instead (it stops when you sign out):"
        & $startup -AtLogon -StartNow
    }
} else {
    & $startup -StartNow
}
Write-Host -NoNewline "    waiting for ComfyUI on port $Port "
for ($i = 0; $i -lt 60; $i++) {
    try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 -Uri "http://127.0.0.1:$Port/system_stats" | Out-Null; break }
    catch { Write-Host -NoNewline "."; Start-Sleep -Seconds 5 }
}
Write-Host ""
Step "Checking the agent"
Run $Venv @("-m", "comfyfarm", "agent", "--config", $AgentYaml, "--check") -AllowFail

Step "Done: $Name"
Note "Logs: $Farm\logs\agent-$Name.log, ComfyUI's: $Farm\logs\comfyui-$Name.log"
Note "It shows on the dashboard's Machines tab once it has checked in."
Stop-Transcript | Out-Null
