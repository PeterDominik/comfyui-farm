"""Runners: what actually runs a job on this machine's GPU, behind one interface (base.py).

The agent knows jobs, the manager and files. A runner knows one backend. Today there is one,
ComfyUI (comfyui.py); docs/decisions/0002 and 0012 say where this is going.
"""


def make_runner(cfg):
    """The runner for this agent's config. Imported here, not at the top: the agent itself
    must not need a backend's packages to be imported."""
    from .comfyui import ComfyUIRunner
    return ComfyUIRunner(cfg)
