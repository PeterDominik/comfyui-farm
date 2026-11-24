"""Runners: what actually runs a job on this machine's GPU, behind one interface (base.py).

The agent knows jobs, the manager and files. A runner knows one backend: ComfyUI
(comfyui.py), resident, and the process runner (process.py), a command per run. Each job
goes to the runner whose definitions it is (decisions 0012, 0025). The farm's gateway has
the API runner alone (api/, decisions 0055, 0056): outside providers, no GPU.
"""


def make_runners(cfg) -> list:
    """This agent's runners, ComfyUI first: it is the one the agent reports the machine's
    backend with. Imported here, not at the top: the agent itself must not need a backend's
    packages to be imported."""
    if cfg.gateway.enabled:
        from .api.runner import ApiRunner
        return [ApiRunner(cfg)]
    from .comfyui import ComfyUIRunner
    from .process import ProcessRunner
    return [ComfyUIRunner(cfg), ProcessRunner(cfg)]

