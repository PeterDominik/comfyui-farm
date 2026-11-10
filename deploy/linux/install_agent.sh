#!/usr/bin/env bash
# A farm agent on a Linux GPU machine, from nothing, without sudo. Run it in the folder that
# should hold everything (fast local disk you own):
#
#   mkdir -p /softwareLocal/comfyfarm && cd /softwareLocal/comfyfarm
#   git clone https://github.com/PeterDominik/comfyui-farm.git
#   bash comfyui-farm/deploy/linux/install_agent.sh
#
# It puts next to each other in that folder:
#   miniconda3/     Miniconda (conda-forge only), with two envs: comfyfarm (the agent) and
#                   comfy (ComfyUI, Python and torch as deploy/comfyui/environment.yaml says)
#   comfyui/        ComfyUI at the farm's commit
#   comfyui-farm/   this repo (cloned if it isn't there yet) and its agent.yaml
# then brings ComfyUI to the farm's (node packs, extra_model_paths.yaml, package pins:
# `comfyfarm comfyui sync`), starts the agent (it starts ComfyUI) and adds the crontab lines
# that keep it running across logouts and reboots.
#
# Safe to re-run: what's there is kept, what's missing is added. A running agent is left
# running (ComfyUI is only brought to the farm's when the agent isn't running, or with
# the machine drained on the dashboard).
#
# Options:
#   --dir DIR         install there instead of the current folder
#   --name NAME       the agent's name (default: <host>-<gpu>, e.g. tor-flame02-a6000)
#   --port PORT       ComfyUI's port (default 8188)
#   --manager M       the manager's manager.json or URL (default /rdo/bank/comfy_farm/manager.json)
#   --branch B        the branch to clone when comfyui-farm/ isn't there (default main)
#   --token-file F    a file with the manager's agents.token. Default: $COMFYFARM_TOKEN, else
#                     the copy in ~/.config/comfyfarm/agent-<name>.yaml, else it asks
#   --no-start        install and check, but don't start the agent or change the crontab
#
# Everything it prints also goes to install_agent.log in the folder.
set -euo pipefail
umask 022

REPO_URL="${COMFYFARM_REPO_URL:-https://github.com/PeterDominik/comfyui-farm.git}"
MINICONDA_URL="${COMFYFARM_MINICONDA_URL:-https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh}"
PYPI="https://pypi.org/simple"
STORAGE="${COMFYFARM_STORAGE:-/rdo/shows /rdo/bank/comfyui /rdo/bank/comfy_farm}"   # must be visible
MIN_FREE_GB="${COMFYFARM_MIN_FREE_GB:-40}"

base="$PWD" name="" port=8188 manager=/rdo/bank/comfy_farm/manager.json branch=main token_file="" start=1
usage() { sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d; s/^# \{0,1\}//' >&2; exit 2; }
while [ $# -gt 0 ]; do
  case "$1" in
    --dir) base="$2"; shift 2 ;;
    --name) name="$2"; shift 2 ;;
    --port) port="$2"; shift 2 ;;
    --manager) manager="$2"; shift 2 ;;
    --branch) branch="$2"; shift 2 ;;
    --token-file) token_file="$2"; shift 2 ;;
    --no-start) start=0; shift ;;
    -h|--help) usage ;;
    *) echo "unknown option: $1" >&2; usage ;;
  esac
done

base="$(cd "$base" && pwd)" || exit 1
cd "$base"
log="$base/install_agent.log"
exec > >(tee -a "$log") 2>&1
echo "===== $(date '+%F %T') install_agent.sh in $base"

step() { printf '\n==> %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
die() { printf '\nSTOPPED: %s\n' "$*"; exit 1; }
# The pipeline's Python settings (rez, PYTHONPATH) must not reach the farm's envs; pip's
# cache stays on this disk rather than in an NFS home.
clean=(env -u PYTHONPATH -u PYTHONHOME -u PYTHONSTARTUP PYTHONNOUSERSITE=1 PIP_CACHE_DIR="$base/pip-cache")

conda_dir="$base/miniconda3"
conda="$conda_dir/bin/conda"
farm="$base/comfyui-farm"
comfy="$base/comfyui"
comfy_env="$conda_dir/envs/comfy"
cpy="$comfy_env/bin/python"

# ---------------------------------------------------------------- the machine
step "Checking this machine"
[ "$(uname -s)" = Linux ] && [ "$(uname -m)" = x86_64 ] || die "this script is for Linux on x86_64"
for t in git curl; do command -v "$t" >/dev/null || die "$t isn't installed"; done
command -v nvidia-smi >/dev/null || die "nvidia-smi isn't there: no NVIDIA driver on this machine?"
gpu="$(nvidia-smi --query-gpu=name --format=csv,noheader | head -n1)"
driver="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -n1)"
gpus="$(nvidia-smi --query-gpu=name --format=csv,noheader | wc -l)"
note "GPU: $gpu, driver $driver"
[ "$gpus" -le 1 ] || note "$gpus GPUs: this installs one agent, for the first one"
fs="$(stat -f -c %T "$base")"
case "$fs" in
  nfs*|cifs|smb*|fuse*|autofs) die "$base is on $fs, a network drive: install on this machine's own disk" ;;
esac
free_gb=$(( $(df -Pk "$base" | awk 'NR==2 {print $4}') / 1048576 ))
note "$base: $fs, $free_gb GB free"
[ "$free_gb" -ge "$MIN_FREE_GB" ] || die "$base has $free_gb GB free; ComfyUI, its Python and the scratch frames need about $MIN_FREE_GB GB"
for d in $STORAGE; do [ -d "$d" ] || die "$d isn't visible from this machine (show storage and the model libraries are)"; done
case "$manager" in
  http://*|https://*) ;;
  *) [ -r "$manager" ] || die "can't read $manager: is the manager running? (or pass --manager http://<host>:<port>)" ;;
esac

if [ -z "$name" ]; then
  short="$(printf '%s' "$gpu" | tr 'A-Z' 'a-z' \
           | sed -e 's/nvidia//; s/geforce//; s/rtx//; s/workstation edition//; s/blackwell//; s/max-q//' \
           | tr -s ' ' | sed -e 's/^ //; s/ $//; s/ /-/g')"
  name="$(hostname -s)-${short:-gpu}"
fi
note "agent name: $name"

# ---------------------------------------------------------------- Miniconda
if [ ! -x "$conda" ]; then
  [ ! -e "$conda_dir" ] || die "$conda_dir exists but has no bin/conda: move it away first"
  step "Installing Miniconda into $conda_dir"
  inst="$base/Miniconda3-latest-Linux-x86_64.sh"
  curl -fL --retry 3 -o "$inst" "$MINICONDA_URL"
  "${clean[@]}" bash "$inst" -b -p "$conda_dir"       # -b: no change to your .bashrc
  rm -f "$inst"
fi
# conda-forge only: Anaconda's own channel asks to accept its Terms of Service (and needs a
# paid licence at a studio this size).
printf 'channels:\n  - conda-forge\n' > "$conda_dir/.condarc"
if "${clean[@]}" "$conda" config --show channels 2>/dev/null | grep -q defaults; then
  note "warning: a ~/.condarc adds Anaconda's 'defaults' channel; envs are made with conda-forge only anyway"
fi

# ---------------------------------------------------------------- the farm's checkout
if [ ! -d "$farm/.git" ]; then
  [ ! -e "$farm" ] || die "$farm exists but isn't a git checkout: move it away first"
  step "Cloning the farm ($branch) into $farm"
  git clone --branch "$branch" "$REPO_URL" "$farm"
else
  step "The farm's checkout: $farm at $(git -C "$farm" rev-parse --short HEAD) ($(git -C "$farm" rev-parse --abbrev-ref HEAD))"
fi

# ---------------------------------------------------------------- agent.yaml
agent_yaml="$farm/agent.yaml"
backup="$HOME/.config/comfyfarm/agent-$name.yaml"
if [ -f "$agent_yaml" ] && ! cmp -s "$agent_yaml" "$farm/agent.example.yaml"; then
  step "agent.yaml is there: kept as it is"
  port="$(sed -n "s|^comfyui:.*:\([0-9][0-9]*\).*|\1|p" "$agent_yaml" | head -n1)"
  port="${port:-8188}"
  kept="$(sed -n "s/^name: *'\{0,1\}\([^' #]*\).*/\1/p" "$agent_yaml" | head -n1)"
  if [ -n "$kept" ] && [ "$kept" != "$name" ]; then name="$kept"; backup="$HOME/.config/comfyfarm/agent-$name.yaml"; fi
  note "agent name: $name, ComfyUI on port $port"
else
  step "Writing agent.yaml"
  if [ -n "$token_file" ]; then
    token="$(tr -d '\r\n' < "$token_file")"
  elif [ -n "${COMFYFARM_TOKEN:-}" ]; then
    token="$COMFYFARM_TOKEN"
  elif [ -r "$backup" ] && grep -q '^token:' "$backup"; then
    token="$(sed -n "s/^token: *'\{0,1\}\([^']*\)'\{0,1\} *$/\1/p" "$backup" | head -n1)"
    note "token from $backup"
  elif [ -t 0 ] || [ -r /dev/tty ]; then
    read -r -s -p "    The manager's agents.token (config.yaml on the manager): " token < /dev/tty; echo
  else
    die "no token: pass --token-file, or set COMFYFARM_TOKEN"
  fi
  [ -n "$token" ] || note "warning: empty token; a manager with agents.token set will refuse this agent"
  if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
    die "port $port is in use on this machine (another ComfyUI?): stop it, or pass --port <another>"
  fi
  q="'"; token_yaml="${token//$q/$q$q}"      # YAML single quotes: ' is written ''
  (umask 077; cat > "$agent_yaml" <<EOF
# Written by deploy/linux/install_agent.sh on $(date +%F). See agent.example.yaml for the rest.
name: $name
manager: '$manager'
token: '$token_yaml'
comfyui: http://127.0.0.1:$port
max_inflight: 2
comfyui_launch:
  command: ['$cpy', main.py, --port, '$port']
  cwd: '$comfy'
EOF
  )
fi
chmod 600 "$agent_yaml"                      # it holds the token
mkdir -p "$(dirname "$backup")"
(umask 077; cp "$agent_yaml" "$backup"; chmod 600 "$backup")
note "a copy is kept in $backup"

# ---------------------------------------------------------------- the farm's Python
step "The agent's Python (conda env comfyfarm)"
bash "$farm/deploy/linux/setup.sh" agent --conda "$conda_dir" --no-check
farmpy="$(cat "$farm/run/python")"

# What the farm's ComfyUI is: deploy/comfyui/environment.yaml
eval "$(cd "$farm" && "${clean[@]}" "$farmpy" - <<'PY'
import shlex
from comfyfarm.comfyenv import machine as M, spec as S
s = S.load() or {}
c, pk = s.get("comfyui") or {}, s.get("packages") or {}
pins = [f"{n}=={pk[n]}" for n in M.FULL_VERSION if isinstance(pk.get(n), str)]
cuda = S.cuda_of(pk.get("torch"))
shared = [sec["library"] + "/" + p.strip("/") for sec in S.farm_model_paths() for p in sec["given"].get("custom_nodes", [])]
for k, v in {"SPEC_COMMIT": c.get("commit") or "", "SPEC_COMFY_URL": c.get("url") or "https://github.com/Comfy-Org/ComfyUI.git",
             "SPEC_VERSION": c.get("version") or "", "SPEC_PYTHON": str(s.get("python") or "3.12"),
             "SPEC_DRIVER_MIN": str(s.get("driver_min") or 0), "SPEC_TORCH": " ".join(pins),
             "SPEC_TORCH_INDEX": f"https://download.pytorch.org/whl/cu{cuda[0]}{cuda[1]}" if cuda else "",
             "SPEC_SHARED_NODES": " ".join(shared)}.items():
    print(f"{k}={shlex.quote(v)}")
PY
)"
[ -n "$SPEC_COMMIT" ] && [ -n "$SPEC_TORCH" ] || die "deploy/comfyui/environment.yaml has no ComfyUI commit or torch"
[ "${driver%%.*}" -ge "$SPEC_DRIVER_MIN" ] \
  || die "NVIDIA driver $driver: the farm's torch ($SPEC_TORCH) needs $SPEC_DRIVER_MIN or newer. Updating it needs IT"

# ---------------------------------------------------------------- ComfyUI
if [ ! -d "$comfy/.git" ]; then
  [ ! -e "$comfy" ] || die "$comfy exists but isn't a git checkout: move it away first"
  step "Cloning ComfyUI $SPEC_VERSION (${SPEC_COMMIT:0:7}) into $comfy"
  git clone --quiet "$SPEC_COMFY_URL" "$comfy"
  git -C "$comfy" checkout --quiet --detach "$SPEC_COMMIT"
else
  step "ComfyUI: $comfy at $(git -C "$comfy" rev-parse --short HEAD 2>/dev/null || echo "an unknown commit") (sync brings it to ${SPEC_COMMIT:0:7})"
fi

if [ ! -x "$cpy" ]; then
  step "ComfyUI's Python (conda env comfy, Python $SPEC_PYTHON)"
  "${clean[@]}" "$conda" create -y -q -p "$comfy_env" --override-channels -c conda-forge "python=$SPEC_PYTHON"
fi
want_torch="${SPEC_TORCH%% *}"; want_torch="${want_torch#torch==}"
have_torch="$("${clean[@]}" "$cpy" -c "import importlib.metadata as m; print(m.version('torch'))" 2>/dev/null || true)"
if [ "$have_torch" != "$want_torch" ]; then
  step "torch for ComfyUI: $SPEC_TORCH (a few GB)"
  # shellcheck disable=SC2086  # the pins are separate words
  "${clean[@]}" "$cpy" -m pip install $SPEC_TORCH ${SPEC_TORCH_INDEX:+--index-url "$SPEC_TORCH_INDEX"}
fi
"${clean[@]}" "$cpy" -c "import torch; assert torch.cuda.is_available(), 'CUDA not available'; print('    torch', torch.__version__, 'on', torch.cuda.get_device_name(0))" \
  || die "torch can't use the GPU (see above)"

step "ComfyUI's requirements, and those of the farm library's shared node packs (torch held)"
held="$farm/run/torch-held.txt"
tr ' ' '\n' <<< "$SPEC_TORCH" > "$held"
reqs=(-r "$comfy/requirements.txt")
for d in $SPEC_SHARED_NODES; do
  for r in "$d"/*/requirements.txt; do [ -f "$r" ] && reqs+=(-r "$r"); done
done
"${clean[@]}" "$cpy" -m pip install --prefer-binary --index-url "$PYPI" -c "$held" "${reqs[@]}"

link="$comfy/custom_nodes/comfyfarm_nodes"
if [ -L "$link" ] || [ ! -e "$link" ]; then
  ln -sfn "$farm/comfyui_nodes/comfyfarm_nodes" "$link"
  note "the farm's nodes: $link -> $farm/comfyui_nodes/comfyfarm_nodes"
else
  die "$link exists and isn't a link to the farm's checkout: move it away and re-run"
fi

# ---------------------------------------------------------------- the farm's ComfyUI
ctl="$farm/deploy/linux/farmctl.sh"
if "$ctl" status agent >/dev/null 2>&1; then
  step "The agent is running: ComfyUI is left as it is"
  note "To bring it to the farm's: drain the machine on the dashboard, then $ctl sync agent --apply"
else
  step "Bringing ComfyUI to the farm's (node packs, extra_model_paths.yaml, package pins)"
  # The agent isn't running, so it holds no jobs and ComfyUI isn't in use: no need to ask
  # the manager. Twice: the second pass pins what the first one's requirements installed.
  "$ctl" sync agent --apply --no-manager || true
  "$ctl" sync agent --apply --no-manager > /dev/null 2>&1 || true
fi
step "How this machine's ComfyUI compares with the farm's"
"$ctl" verify agent || note "(the differences above are shown on the dashboard too; the agent still runs)"

# ---------------------------------------------------------------- start
if [ "$start" = 0 ]; then
  step "Installed. Not started (--no-start). To start it and keep it running:"
  note "$ctl start agent && $farm/deploy/linux/install_cron.sh agent"
  exit 0
fi
step "Starting the agent (it starts ComfyUI)"
"$ctl" ensure agent
printf '    waiting for ComfyUI on port %s ' "$port"
for _ in $(seq 1 60); do
  curl -fs "http://127.0.0.1:$port/system_stats" > /dev/null 2>&1 && break
  printf '.'; sleep 5
done
echo
step "Checking the agent"
"$ctl" check agent || note "(fix what's marked FAIL, then: $ctl check agent)"
step "Keeping it running (your crontab: at boot, and checked every minute)"
"$farm/deploy/linux/install_cron.sh" agent

step "Done: $name"
note "Logs: $farm/logs/agent-$name.log, ComfyUI's: $farm/logs/comfyui-$name.log"
note "It shows on the dashboard's Machines tab once it has checked in."
