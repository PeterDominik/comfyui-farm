# 0024. Runner environments are uv projects in the repo, built on each machine with uv

Date: 2026-11-21. Status: accepted.

## Context

The process runner (phase 3) runs a job's Python, and later runners (training, Griptape)
do the same. Each needs an environment the farm controls: the same packages on every
machine, built from the reviewed repo, never from what a job sends. The plan left the tool
that builds them to a test on the three pilot machines (`tools/builder_test.py`): uv, or
the installers' method (conda on Linux, a python.org venv on Windows), each building the
same Python 3.12, torch 2.13.0+cu130, numpy and pillow, cold then warm.

| Machine | uv: torch cold, warm | uv: disk, two builds and cache | Installers' method |
|---|---|---|---|
| wrk1125l (3090, Linux) | 29 s, 1 s | 14.1 GB (copied) | failed: its Miniconda Python has no pip |
| tor-flame02 (A6000, Linux) | 29 s, 2 s | 14.1 GB (copied) | passed: torch 194 s, 177 s; 10.7 GB |
| wrk59467w (5090, Windows), ComfyUI's Python | 36 s, 7 s | 3.0 GB (linked) | passed: torch 106 s, 83 s; 7.9 GB |
| wrk59467w, the farm's Python 3.11 | 37 s, 7 s | 3.0 GB (linked) | failed: a venv of Python 3.11 can't take numpy 2.5.2 |

uv passed everywhere, with its own Python whatever the machine has, and was faster. The
installers' method depended on the machine's Python and failed on two of the four runs.
Both Linux machines carry the studio's pip mirror settings and `REZ_CONFIG_FILE`; neither
reached a uv build.

On Linux uv copied packages instead of linking them from its cache, so every build took
its full size. uv's default link mode is `clone` (copy-on-write) on Linux and `hardlink`
on Windows. Neither Linux disk can clone, so uv copied; Windows linked.

## Decision

- **uv builds every runner environment.** Version 0.13.9, the one tested, is installed
  with pip from pypi.org into `<envs.dir>/.uv-tool`: nothing on PATH, no admin rights.
  `envs.uv` in `agent.yaml` names another uv instead.
- **An environment is a folder in the repo**, `environments/<name>/`, a uv project:
  `pyproject.toml` (Python, packages; PyPI as the default index; PyTorch's index marked
  `explicit` and used for torch only; `[tool.uv] package = false`; resolved for Linux and
  Windows only), `.python-version`, `env.yaml` (what it is, and a check command) and
  `uv.lock`. All four are reviewed like code.
- **The lock is made once and committed.** `python -m comfyfarm env lock <name>` needs
  pypi.org and the PyTorch index; it runs on a machine that reaches them, and the lock goes
  into the same pull request as the change. Machines build with `uv sync --frozen`: exactly
  the lock, never resolved again, so every machine of an OS has the same packages.
- **A build is named by the folder's fingerprint**: the four files, line endings aside, so a
  Windows checkout gives the same name. `env install <name>` builds into
  `<envs.dir>/<name>/<fingerprint>/`, runs the check with the build's Python, and only
  then writes `ok` and points `current` at it. The build it replaces becomes `previous`;
  older ones are removed. A build that fails leaves `current` untouched and is deleted.
  Going back to the previous version needs no build.
- **What a build is made with is fixed:** uv's own Python (`only-managed`), its cache and
  Pythons under `envs.dir`, `UV_LINK_MODE=hardlink`. The pipeline's `PYTHONPATH`,
  `PYTHONHOME` and user site, and any uv index settings on the machine, are left out of
  every uv call and every check. `farm-env.json` in each build records how long it took,
  what the check printed, and whether uv still had to copy (`copied`).
- **For now a person builds**, on each machine, with `env install`. The process runner will
  ask for an environment by name and run in its `current` build. Building from the
  manager, with canary and rollback, is phase 4. The farm's ComfyUI keeps
  `deploy/comfyui/environment.yaml` and `comfyui sync` until then.

## Consequences

- A change of package is a pull request with its new `uv.lock`, so the review sees every
  package that changes, torch's dependencies included.
- A torch environment is about 4.6 GB on Linux and 2.9 GB on Windows. With hard links a
  second build that shares torch costs little, but only if `envs.dir` and uv's cache are on
  the same disk, which they are by default. `copied: true` shows where linking failed.
- Locking can't happen on a machine without pypi.org. Building can, if uv's cache already
  holds the packages; otherwise it needs pypi.org and download.pytorch.org too, as the
  builder test did.
- `comfyfarm/envs/` knows no runner, and no runner is in the layering rules for it yet.
