"""This machine's NVIDIA GPUs, as the driver reports them (docs/decisions/0022).

The agent reads them itself, with nvidia-smi, which comes with the driver on Linux and on
Windows: a runner without a GPU API of its own (the process runner) still knows the card,
and the manager sees memory any program holds on it, not only ComfyUI's. Nothing here
needs a Python package or a runner."""

from __future__ import annotations

import os
import re
import subprocess

FIELDS = ("index", "uuid", "name", "memory.total", "memory.used", "memory.free", "utilization.gpu",
          "driver_version")
MIB = 2**20


def _number(text: str) -> int | None:
    """nvidia-smi's numbers: '[N/A]' or '[Not Supported]' when a card doesn't say."""
    try:
        return int(float(text))
    except ValueError:
        return None


def parse(text: str) -> list[dict]:
    """nvidia-smi's CSV (no header, no units), one GPU a line. Lines it can't read are left out."""
    gpus = []
    for line in text.splitlines():
        cols = [c.strip() for c in line.split(",")]
        if len(cols) != len(FIELDS) or _number(cols[0]) is None:
            continue
        mem = [_number(c) for c in cols[3:6]]
        gpus.append({"index": _number(cols[0]), "uuid": cols[1], "name": cols[2],
                     "vram_total": mem[0] * MIB if mem[0] is not None else None,
                     "vram_used": mem[1] * MIB if mem[1] is not None else None,
                     "vram_free": mem[2] * MIB if mem[2] is not None else None,
                     "utilization": _number(cols[6]), "driver": cols[7]})
    return gpus


def device_name(name: str | None) -> str | None:
    """A runner's name for its device without the device number and allocator:
    'cuda:0 NVIDIA GeForce RTX 5090 : cudaMallocAsync' -> 'NVIDIA GeForce RTX 5090'."""
    name = re.sub(r"\s+:\s+\S+\s*$", "", re.sub(r"^\s*cuda:\d+\s+", "", name or "")).strip()
    return name or None


def probe(command: list[str] | None = None, timeout: float = 10.0) -> tuple[list[dict], str | None]:
    """(the GPUs, None), or ([], what went wrong) in words a person can act on."""
    cmd = [*(command or ["nvidia-smi"]), f"--query-gpu={','.join(FIELDS)}", "--format=csv,noheader,nounits"]
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}   # no console window
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, **flags)
    except FileNotFoundError:
        return [], (f"{cmd[0]} isn't installed or isn't on PATH: it comes with the NVIDIA driver "
                    f"(or set gpu_probe.command in agent.yaml)")
    except subprocess.TimeoutExpired:
        return [], f"{cmd[0]} didn't answer in {timeout:g} s"
    except OSError as e:
        return [], f"{cmd[0]} couldn't be started: {e}"
    if r.returncode != 0:
        said = (r.stderr or r.stdout or "").strip().splitlines()
        return [], f"{cmd[0]} failed: {said[-1] if said else f'exit {r.returncode}'}"
    gpus = parse(r.stdout)
    return gpus, None if gpus else f"{cmd[0]} listed no GPU"
