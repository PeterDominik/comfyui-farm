"""Dispatching a staged template to Tractor through Daiquiri.

The command is config.yaml's publish.command, run in a clean login shell (publish.shell):
the manager runs from cron with the farm's own Python, while dispatchGraph needs the
studio's pipeline environment. `ctxl` and `dispatchGraph` are bash aliases, which a
non-interactive shell doesn't expand, so the default command sources the show's context
(set_context.sh) and calls the launcher they stand for:

  source /rdo/rodeo/setup/rdo_rez_launcher/current/env/set_context.sh {show} &&
  /rdo/rodeo/setup/rdo_rez_launcher/current/rdo_rez_launcher.bash -a graph -c dispatchGraph
      update_and_render_nuke --description {description} --step {step} --entities {shot}
      --workfile {workfile} --frameRange {frames}

Every value is shell-quoted before it goes in. A dry run adds dispatchGraph's own
--dry-run: Daiquiri checks the context and the workfile and sends nothing to Tractor.
dispatchGraph prints the Tractor job it made ("Dispatched engine, #ID: 6501887.") and its
title ("jobTitle: [Nuke Render] …")."""

from __future__ import annotations

import getpass
import os
import re
import shlex
import subprocess

__all__ = ["command", "run", "parse"]


def command(shell: list[str], template: str, values: dict) -> list[str]:
    """The argv to run: the shell, then the command with every value quoted."""
    quoted = {k: shlex.quote(str(v)) for k, v in values.items()}
    try:
        text = template.format(**quoted)
    except KeyError as e:
        raise ValueError(f"publish.command uses {{{e.args[0]}}}, which isn't one of {', '.join(sorted(values))}") from None
    env = {"home": os.path.expanduser("~"), "user": getpass.getuser()}
    return [part.format(**env) for part in shell] + [text]


def run(argv: list[str], timeout: float) -> tuple[int, str]:
    """(exit code, output). The output is kept for the publish record."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, stdin=subprocess.DEVNULL)
    except FileNotFoundError as e:
        return 127, f"{argv[0]}: {e}"
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") if isinstance(e.stdout, str) else ""
        return 124, out + f"\n(stopped after {timeout:.0f} s)"
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def parse(output: str, job_pattern: str) -> dict:
    """The Tractor job id and title from dispatchGraph's output."""
    out = {}
    m = re.search(job_pattern, output or "")
    if m:
        out["tractor_id"] = m.group(1)
    m = re.search(r"jobTitle:\s*(.+)", output or "")
    if m:
        out["title"] = m.group(1).strip()
    return out
