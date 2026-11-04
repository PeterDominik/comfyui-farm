#!/usr/bin/env bash
# First-time install on a Linux machine, without sudo (safe to re-run):
#
#   deploy/linux/setup.sh manager [--conda DIR] [--env DIR]
#   deploy/linux/setup.sh agent   [--conda DIR] [--env DIR] [--comfyui DIR]
#
#   --conda DIR    your Miniconda (default: the conda on PATH, else ~/miniconda3)
#   --env DIR      the farm's conda env (default: <conda>/envs/comfyfarm), kept apart from
#                  the pipeline's Python
#   --comfyui DIR  agent: ComfyUI's folder; links the farm's ComfyUI nodes into its custom_nodes
#
# Creates the env (Python 3.12) and installs requirements.txt into it, copies
# config.example.yaml (manager) or agent.example.yaml (agent) if there's no config yet,
# and runs the check. Then: farmctl.sh start <role>, and install_cron.sh <role> to keep it
# running. Keep the checkout on local disk (data/, logs/ and scratch go next to it).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
role="${1:-}"
case "$role" in manager|agent) shift ;; *) echo "usage: $0 manager|agent [--conda DIR] [--env DIR] [--comfyui DIR]" >&2; exit 2 ;; esac
conda_dir="" env_dir="" comfyui=""
while [ $# -gt 0 ]; do
  case "$1" in
    --conda) conda_dir="$2"; shift 2 ;;
    --env) env_dir="$2"; shift 2 ;;
    --comfyui) comfyui="$2"; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

if [ -z "$conda_dir" ]; then
  if [ -n "${CONDA_EXE:-}" ]; then conda_dir="$(dirname "$(dirname "$CONDA_EXE")")"
  elif command -v conda >/dev/null 2>&1; then conda_dir="$(dirname "$(dirname "$(command -v conda)")")"
  else conda_dir="$HOME/miniconda3"; fi
fi
conda="$conda_dir/bin/conda"
if [ ! -x "$conda" ]; then
  echo "No conda at $conda_dir. Pass --conda <your Miniconda folder>, or install Miniconda without sudo:" >&2
  echo "  bash Miniconda3-latest-Linux-x86_64.sh -b -p <local disk>/miniconda3" >&2
  exit 1
fi
env_dir="${env_dir:-$conda_dir/envs/comfyfarm}"
py="$env_dir/bin/python"

# The pipeline's Python settings (rez, PYTHONPATH) must not reach the farm's env.
clean=(env -u PYTHONPATH -u PYTHONHOME -u PYTHONSTARTUP PYTHONNOUSERSITE=1)

if [ ! -x "$py" ]; then
  echo "Creating the conda env $env_dir (Python 3.12)..."
  "${clean[@]}" "$conda" create -y -q -p "$env_dir" python=3.12
fi
echo "Installing requirements into $env_dir..."
"${clean[@]}" "$py" -m pip install -q --upgrade pip
"${clean[@]}" "$py" -m pip install -q -r "$ROOT/requirements.txt"

umask 022
mkdir -p "$ROOT/data" "$ROOT/logs" "$ROOT/run"
echo "$py" > "$ROOT/run/python"          # farmctl.sh runs this Python

cfg="config.yaml"; [ "$role" = agent ] && cfg="agent.yaml"
if [ ! -f "$ROOT/$cfg" ]; then
  cp "$ROOT/${cfg%.yaml}.example.yaml" "$ROOT/$cfg"
  echo "Created $cfg from its example: edit it before going live."
fi

if [ -n "$comfyui" ]; then
  custom=""
  for c in "$comfyui/custom_nodes" "$comfyui/ComfyUI/custom_nodes" "$comfyui"; do
    if [ -d "$c" ] && [ "$(basename "$c")" = custom_nodes ]; then custom="$c"; break; fi
  done
  [ -n "$custom" ] || { echo "No custom_nodes folder in $comfyui (pass ComfyUI's folder)" >&2; exit 1; }
  link="$custom/comfyfarm_nodes"
  target="$ROOT/comfyui_nodes/comfyfarm_nodes"
  if [ -L "$link" ] || [ ! -e "$link" ]; then
    ln -sfn "$target" "$link"
    echo "Linked the farm's ComfyUI nodes: $link -> $target (restart ComfyUI to load them)"
  else
    echo "$link exists and isn't a link to this repo: move it away and re-run" >&2; exit 1
  fi
fi

echo
if [ "$role" = manager ]; then
  "${clean[@]}" "$py" -m comfyfarm --config "$ROOT/config.yaml" check || true
  cat <<EOT

Next:
  Edit config.yaml (share.dir, agents.token, paths), check again:
                   $ROOT/deploy/linux/farmctl.sh check manager
  Start it:        $ROOT/deploy/linux/farmctl.sh start manager
  Keep it running: $ROOT/deploy/linux/install_cron.sh manager
EOT
else
  "${clean[@]}" "$py" -m comfyfarm agent --config "$ROOT/agent.yaml" --check || true
  cat <<EOT

Next:
  Edit agent.yaml (name, manager, token, comfyui_launch), check again:
                   $ROOT/deploy/linux/farmctl.sh check agent
  Start it:        $ROOT/deploy/linux/farmctl.sh start agent
  Keep it running: $ROOT/deploy/linux/install_cron.sh agent
EOT
fi
