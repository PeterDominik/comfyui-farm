"""Runner environments: the Pythons a runner's jobs run in (docs/decisions/0024).

Each one is a folder in environments/ of this repo, a small uv project: its
pyproject.toml (Python version, packages, which index each comes from), its
.python-version, env.yaml (what it is and the command that checks a build of it) and
uv.lock (every package pinned, with its hashes, for Linux and Windows).

- spec.py reads a folder and its fingerprint.
- build.py locks it (on a machine that reaches pypi.org), builds this machine's copy with
  uv, checks it, and keeps the current and the previous build.
- cli.py is `python -m comfyfarm env list | lock | install | remove`.

Nothing here knows any runner: the process runner asks for an environment by name."""
