"""Stand-ins for uv, conda and pip, for tools/builder_test.py's and comfyfarm/envs' tests: no network.

    python fake_builder.py uv <uv's arguments>
    python fake_builder.py conda <conda's arguments>

Environments are real venvs (made without pip), so the builder test's check runs in them.
"Installing" a package writes a stub of it (torch says it sees a GPU) into the
environment. Every call is logged as a JSON line to $FAKE_BUILDER_LOG. FAKE_BUILDER_FAIL
names a step to fail (e.g. "uv pip"). FAKE_UV_COPIES=1 makes `uv sync` say, as uv does,
that it couldn't hard-link from its cache.
"""

import json
import tomllib
import os
import subprocess
import sys
import sysconfig
from pathlib import Path

# A stub `pip` written into the environments conda makes, so `python -m pip install` works.
PIP_MAIN = r'''
import json, os, sys, sysconfig
sys.path.insert(0, os.environ["FAKE_BUILDER_DIR"])
import fake_builder
fake_builder.log("pip", sys.argv[1:])
fake_builder.fail_if("pip")
fake_builder.install(sysconfig.get_paths()["purelib"], sys.argv[1:])
'''


def log(tool: str, args: list) -> None:
    path = os.environ.get("FAKE_BUILDER_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"tool": tool, "args": args, "env": {k: os.environ.get(k) for k in (
                "UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR", "UV_PYTHON_PREFERENCE", "UV_NO_CONFIG", "PIP_CACHE_DIR",
                "PYTHONPATH", "PYTHONNOUSERSITE", "UV_PROJECT_ENVIRONMENT", "UV_LINK_MODE", "UV_INDEX_URL")}}) + "\n")


def fail_if(step: str) -> None:
    if os.environ.get("FAKE_BUILDER_FAIL") == step:
        print(f"{step}: failed on purpose", file=sys.stderr)
        sys.exit(1)


def install(purelib: str, args: list) -> None:
    """Write a stub for each name==version in the arguments."""
    for a in args:
        if a.startswith("-") or "==" not in a and a not in ("numpy", "pillow"):
            continue
        name, _, version = a.partition("==")
        version = version or "0.0"
        module = {"pillow": "PIL"}.get(name, name)
        folder = Path(purelib) / module
        folder.mkdir(parents=True, exist_ok=True)
        body = f"__version__ = {version!r}\n"
        if module == "torch":
            body += ("class cuda:\n    is_available = staticmethod(lambda: True)\n"
                     "    get_device_name = staticmethod(lambda i=0: 'Fake GPU')\n")
        (folder / "__init__.py").write_text(body, encoding="utf-8")


def purelib_of(python: str) -> str:
    return subprocess.run([python, "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
                          capture_output=True, text=True, check=True).stdout.strip()


def make_env(folder: str, with_pip: bool) -> None:
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", folder], check=True)
    if with_pip:
        py = next(p for p in (Path(folder) / "bin/python", Path(folder) / "Scripts/python.exe") if p.exists())
        pip = Path(purelib_of(str(py))) / "pip"
        pip.mkdir(parents=True, exist_ok=True)
        (pip / "__init__.py").write_text("", encoding="utf-8")
        (pip / "__main__.py").write_text(PIP_MAIN, encoding="utf-8")


def uv(args: list) -> None:
    if args[:1] == ["--version"]:
        print("uv 0.0.0 (fake)")
    elif args[:2] == ["python", "install"]:
        fail_if("uv python")
        Path(os.environ["UV_PYTHON_INSTALL_DIR"], f"cpython-{args[2]}").mkdir(parents=True, exist_ok=True)
    elif args[:1] == ["venv"]:
        fail_if("uv venv")
        make_env(args[-1], with_pip=False)
    elif args[:2] == ["pip", "install"]:
        fail_if("uv pip")
        py = args[args.index("--python") + 1]
        Path(os.environ["UV_CACHE_DIR"]).mkdir(parents=True, exist_ok=True)
        install(purelib_of(py), args[2:])
    elif args[:1] == ["lock"]:
        fail_if("uv lock")
        project = Path(args[args.index("--project") + 1])
        meta = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        deps = meta["dependencies"]
        # The part of a real lock the farm reads: what the project asked for when it was locked.
        reqs = []
        for d in deps:
            name, _, version = d.partition("==")
            reqs.append(f'{{ name = "{name.strip()}", specifier = "=={version.strip()}" }}' if version
                        else f'{{ name = "{name.strip()}" }}')
        (project / "uv.lock").write_text(
            f'version = 1\nrequires-python = "{meta.get("requires-python", "")}"\n\n[[package]]\n'
            f'name = "{meta["name"]}"\nversion = "{meta.get("version", "0")}"\nsource = {{ virtual = "." }}\n\n'
            f'[package.metadata]\nrequires-dist = [{", ".join(reqs)}]\n', encoding="utf-8")
        print(f"Resolved {len(deps)} packages (fake)")
    elif args[:1] == ["sync"]:
        fail_if("uv sync")
        project = Path(args[args.index("--project") + 1])
        if "--frozen" in args and not (project / "uv.lock").exists():
            sys.exit("error: Unable to find lockfile at `uv.lock`, but `--frozen` was provided")
        target = os.environ["UV_PROJECT_ENVIRONMENT"]
        if not Path(target).exists():
            make_env(target, with_pip=False)
        py = next(p for p in (Path(target) / "bin/python", Path(target) / "Scripts/python.exe") if p.exists())
        Path(os.environ["UV_CACHE_DIR"]).mkdir(parents=True, exist_ok=True)
        deps = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
        install(purelib_of(str(py)), deps)
        if os.environ.get("FAKE_UV_COPIES"):
            print("warning: Failed to hardlink files; falling back to full copy. This may lead to degraded "
                  "performance.", file=sys.stderr)
        print(f"Installed {len(deps)} packages (fake)")
    else:
        sys.exit(f"fake uv: {args} isn't faked")


def conda(args: list) -> None:
    if args[:1] == ["create"]:
        fail_if("conda create")
        make_env(args[args.index("-p") + 1], with_pip=True)
    else:
        sys.exit(f"fake conda: {args} isn't faked")


if __name__ == "__main__":
    os.environ.setdefault("FAKE_BUILDER_DIR", str(Path(__file__).resolve().parent))
    tool, rest = sys.argv[1], sys.argv[2:]
    log(tool, rest)
    {"uv": uv, "conda": conda}[tool](rest)
