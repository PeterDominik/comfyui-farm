#!/usr/bin/env bash
# Keep the manager or this machine's agent running with your own crontab (no sudo):
# started when the machine boots, and checked every minute (started again if it stopped).
#
#   deploy/linux/install_cron.sh manager|agent [agent.yaml]
#   deploy/linux/install_cron.sh manager|agent --remove
#
# Only the farm's own lines (marked "# comfyfarm-<role>") are touched.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
role="${1:-}"
case "$role" in manager|agent) ;; *) echo "usage: $0 manager|agent [agent.yaml | --remove]" >&2; exit 2 ;; esac
arg="${2:-}"
mark="# comfyfarm-$role"
ctl="$ROOT/deploy/linux/farmctl.sh"
conf=""
[ -n "$arg" ] && [ "$arg" != --remove ] && conf=" $(cd "$(dirname "$arg")" && pwd)/$(basename "$arg")"

if ! current="$(crontab -l 2>&1)"; then
  case "$current" in
    *"no crontab"*) current="" ;;
    *) echo "Can't use crontab here: $current" >&2
       echo "Start it by hand instead: $ctl start $role" >&2
       exit 1 ;;
  esac
fi
kept="$(printf '%s\n' "$current" | grep -v -F "$mark" || true)"
if [ "$arg" = --remove ]; then
  printf '%s\n' "$kept" | sed '/^$/d' | crontab -
  echo "Removed the $role lines from your crontab (it keeps running until: $ctl stop $role)."
  exit 0
fi
{
  printf '%s\n' "$kept" | sed '/^$/d'
  echo "@reboot $ctl ensure $role$conf >/dev/null 2>&1 $mark"
  echo "* * * * * $ctl ensure $role$conf >/dev/null 2>&1 $mark"
} | crontab -
echo "Your crontab now starts the $role at boot and checks it every minute:"
crontab -l | grep -F "$mark"
"$ctl" ensure "$role"${conf:+ $conf}
