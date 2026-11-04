#!/usr/bin/env bash
# Run the farm's manager or this machine's agent in the background, without sudo:
#
#   deploy/linux/farmctl.sh start|stop|restart|status|ensure|log|check  manager|agent [agent.yaml]
#   deploy/linux/farmctl.sh stop|status comfyui      the ComfyUI the agent started (comfyui_launch)
#
#   ensure   start it if it isn't running (install_cron.sh runs this every minute)
#   log      follow its console output (the full log is logs/comfyfarm.log, logs/agent-<name>.log)
#   check    comfyfarm check (manager) or comfyfarm agent --check (agent)
#
# Runs with the Python setup.sh recorded (run/python), or $COMFYFARM_PYTHON, with the
# pipeline's Python settings removed. Stopping the agent leaves ComfyUI and its jobs running.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cmd="${1:-}" role="${2:-}" conf="${3:-}"
usage() { echo "usage: $0 start|stop|restart|status|ensure|log|check manager|agent [agent.yaml]" >&2; exit 2; }
[ -n "$cmd" ] && [ -n "$role" ] || usage

py="${COMFYFARM_PYTHON:-$(cat "$ROOT/run/python" 2>/dev/null || true)}"
case "$role" in
  manager) args=(-m comfyfarm --config "${conf:-$ROOT/config.yaml}" manager)
           check=(-m comfyfarm --config "${conf:-$ROOT/config.yaml}" check) ;;
  agent)   args=(-m comfyfarm agent --config "${conf:-$ROOT/agent.yaml}")
           check=(-m comfyfarm agent --config "${conf:-$ROOT/agent.yaml}" --check) ;;
  comfyui) ;;
  *) usage ;;
esac
mkdir -p "$ROOT/run" "$ROOT/logs"
pidfile="$ROOT/run/$role.pid"
out="$ROOT/logs/$role.out"
clean=(env -u PYTHONPATH -u PYTHONHOME -u PYTHONSTARTUP PYTHONNOUSERSITE=1 PYTHONUTF8=1)

running() {   # prints the pid if it runs
  local pid
  pid="$(cat "$pidfile" 2>/dev/null)" || return 1
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null || return 1
  if [ -r "/proc/$pid/cmdline" ] && ! tr '\0' ' ' < "/proc/$pid/cmdline" | grep -q -- "$role"; then
    return 1      # that pid is something else now
  fi
  echo "$pid"
}

comfyui_pids() {
  local f pid
  for f in "$ROOT"/logs/comfyui-*.pid; do
    [ -f "$f" ] || continue
    pid="$(cat "$f")"
    kill -0 "$pid" 2>/dev/null && echo "$pid $f"
  done
}

start() {
  local pid
  if pid="$(running)"; then echo "$role is running (pid $pid)"; return 0; fi
  [ -x "$py" ] || { echo "No Python at '$py': run deploy/linux/setup.sh $role first" >&2; return 1; }
  cd "$ROOT" || return 1
  if [ -f "$out" ] && [ "$(stat -c %s "$out" 2>/dev/null || echo 0)" -gt 52428800 ]; then
    mv -f "$out" "$out.1"            # console output only; the real log rotates itself
  fi
  echo "===== $(date '+%F %T') starting $role" >> "$out"
  # setsid: its own session, so closing the terminal (or cron finishing) doesn't stop it.
  # umask 022: what it writes is readable by everyone.
  umask 022
  setsid "${clean[@]}" "$py" "${args[@]}" >> "$out" 2>&1 < /dev/null 9>&- &
  echo $! > "$pidfile"
  sleep 1
  if pid="$(running)"; then echo "$role started (pid $pid), output in $out"; return 0; fi
  echo "$role exited at once; the end of $out:" >&2
  tail -n 20 "$out" >&2
  return 1
}

stop() {
  local pid i
  if ! pid="$(running)"; then echo "$role isn't running"; rm -f "$pidfile"; return 0; fi
  kill -TERM "$pid"
  for i in $(seq 1 30); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
  kill -0 "$pid" 2>/dev/null && kill -KILL "$pid"
  rm -f "$pidfile"
  echo "$role stopped"
}

case "$role:$cmd" in
  comfyui:status) comfyui_pids | while read -r pid f; do echo "ComfyUI running (pid $pid, $f)"; done ;;
  comfyui:stop)   comfyui_pids | while read -r pid f; do kill -TERM "$pid" && echo "stopped ComfyUI (pid $pid)"; done ;;
  comfyui:*)      usage ;;
  *:start)   start ;;
  *:stop)    stop ;;
  *:restart) stop; start ;;
  *:ensure)
    exec 9> "$ROOT/run/$role.lock"
    flock -n 9 || exit 0             # another ensure is on it
    running > /dev/null || start ;;
  *:status)
    if pid="$(running)"; then echo "$role is running (pid $pid)"; else echo "$role isn't running"; exit 3; fi ;;
  *:log)     tail -n 50 -F "$out" ;;
  *:check)   cd "$ROOT" && "${clean[@]}" "$py" "${check[@]}" ;;
  *) usage ;;
esac
