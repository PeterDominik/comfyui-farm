# Builder test

Phase 3 builds runner environments (the process runner's, later ComfyUI's) from a spec
in git. Which tool builds them is chosen by this test, on each of the three pilot machines:
uv if it passes on all three, otherwise the installers' method (docs/plan.md).

`python tools/builder_test.py` builds the same small environment twice with each method,
cold then warm: the Python and torch that `deploy/comfyui/environment.yaml` pins, and
numpy and pillow from pypi.org. It times every step and checks each environment.

| Method | Python | Packages |
|---|---|---|
| uv | `uv python install` (uv's own Python, downloaded from GitHub) and `uv venv` | `uv pip install`, torch from the PyTorch index, the rest from pypi.org |
| installers | Linux: `conda create` from the installer's Miniconda (conda-forge). Windows: a venv of python.org's Python | `pip install`, the same indexes |

uv is installed with pip into the test's own folder; nothing goes on PATH and no admin
rights are needed. Every step runs without the pipeline's Python settings (rez,
`PYTHONPATH`), as the farm's start-up scripts do.

## Running it

On each machine, from the farm's checkout, with the farm's Python (`run/python` on Linux,
`.venv\Scripts\python.exe` on Windows). The farm keeps running: the test touches none of
its environments.

1. A quick run first (a few minutes, a few hundred MB):
   `python tools/builder_test.py --no-torch`
2. The real one (torch is a few GB per environment, so about 20 GB free and up to an hour
   on a cold cache):
   `python tools/builder_test.py`
3. Commit `run/builder-test/<machine>.json` here as `docs/builder/<machine>.json`, or paste
   the summary it prints into the pull request or the chat.

Everything is built in `run/builder-test/` and removed at the end (`--keep` keeps it).
`--method uv` or `--method installers` runs one. `--uv <uv>` uses a uv the machine already
has, and `--conda <conda>` a conda other than the installer's.

## What passes

A method passes on a machine when both of its builds:

- run every step without an error;
- have the pinned Python version (`python` in the environment file);
- import torch at the pinned version, and torch sees the GPU (skipped with `--no-torch`);
- import numpy and pillow;
- have nothing on their import path from outside the environment and its Python (a
  pipeline path left over in a `.pth` file or a site folder fails it).

The report also gives each step's time, the disk each environment takes, the disk both
environments and the method's cache take together (uv links files from its cache, so its
second environment costs little), and which pip and uv settings the machine has (their
names only, not the studio mirror's address).

## Results

uv passed on all three machines; the installers' method failed on two of the four runs
(the 3090's Miniconda Python has no pip; Windows' Python 3.11 can't take numpy 2.5.2). uv
is the builder: see `docs/decisions/0024-environments-are-uv-projects.md` for the numbers,
and `environments/` and `python -m comfyfarm env` for how environments are built now.

| Report | Machine |
|---|---|
| `wrk1125l.rodeofx.com_torch.json` | 3090, Linux |
| `tor-flame02_torch.json` | A6000, Linux |
| `wrk59467w_torch_condaPython.json` | 5090, Windows, run with ComfyUI's Python |
| `wrk59467w_torch_defaultPython.json` | 5090, Windows, run with the farm's Python 3.11 |
| `<machine>.json` | the same machines, `--no-torch` |

On both Linux machines uv copied packages instead of linking them from its cache (14.1 GB
for two builds and the cache, against 3.0 GB on Windows): uv clones on Linux by default,
and those disks can't. `env install` asks uv for hard links instead.
