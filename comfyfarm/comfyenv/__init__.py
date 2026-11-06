"""The farm's ComfyUI: one environment for every farm machine (chunk 6).

  spec.py     deploy/comfyui/environment.yaml (written from a reference machine with
              `comfyfarm comfyui snapshot`) and how a machine differs from it
  machine.py  what a machine's ComfyUI is: version and launch flags, Python packages,
              driver, node packs, extra_model_paths.yaml, the farm's models
  sync.py     bringing a machine to the file: ComfyUI's commit, node packs, packages
              (torch held), extra_model_paths.yaml
  cli.py      comfyfarm comfyui snapshot | verify | sync

Agents collect their machine's state and send it to the manager, which compares it with
the environment file; the Machines tab shows the differences. A machine that differs
keeps taking jobs (missing nodes or models still keep a workflow off it, as before)."""

from . import machine, spec
from .machine import collect
from .spec import compare

__all__ = ["machine", "spec", "collect", "compare"]
