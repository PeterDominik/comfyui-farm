"""Phase 3's builder test (docs/plan.md, docs/builder/README.md): can uv build the farm's
runner environments on this machine, and how does it compare with the installers' method?

    python tools/builder_test.py                  # both methods, torch included (needs ~20 GB free)
    python tools/builder_test.py --no-torch       # a quick first run: Python, numpy and pillow only
    python tools/builder_test.py --method uv      # one method

Each method builds the same small environment twice, cold then warm (its cache filled):
the Python and torch that deploy/comfyui/environment.yaml pins, and numpy and pillow from
pypi.org. Every step is timed, and each environment is checked: the Python version, torch
seeing the GPU, and no import path leaking in from the pipeline (rez, PYTHONPATH).

- uv: installed with pip into a folder of its own (or --uv <uv>); it downloads its own
  Python, ignores pip's settings and links packages from one cache.
- installers: what deploy/*/install_agent does today. On Linux a conda-forge env from the
  installer's Miniconda (found from this machine's agent, or --conda <conda>); on Windows a
  venv of python.org's Python (--base-python, default this Python's own). Then pip, told
  the PyTorch index for torch and pypi.org for the rest.

Everything goes into run/builder-test/ (not committed) and is removed at the end unless
--keep. The report is written to run/builder-test/<machine>.json and summed up on screen;
commit that file to docs/builder/. Nothing is installed anywhere else, and the farm keeps
running: the test touches no environment of the farm's.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "comfyfarm").is_dir())
sys.path.insert(0, str(ROOT))

from comfyfarm.comfyenv import spec as S  # noqa: E402
from comfyfarm.comfyenv.sync import PYPI, clean_env  # noqa: E402

WINDOWS = os.name == "nt"
METHODS = ("uv", "installers")
SMALL = ("numpy", "pillow")             # from pypi.org, pinned as the environment file pins them
TIMEOUT = 3600                          # seconds a step may take: torch is a few GB
PY = "<env-python>"                     # in a step: the environment's Python, found once it exists

# Run inside each environment built: what it is, what it sees, and whether anything from
# outside it and its own Python is on its import path.
CHECK = r"""
import json, os, sys, sysconfig
out = {"python": "%d.%d.%d" % sys.version_info[:3], "prefix": sys.prefix}
own = [os.path.normcase(os.path.realpath(p)) for p in (sys.prefix, sys.base_prefix, sys.exec_prefix)]
out["foreign_paths"] = [p for p in sys.path if p and os.path.isdir(p)
                        and not any(os.path.normcase(os.path.realpath(p)).startswith(o) for o in own)]
for mod, attr in (("numpy", "__version__"), ("PIL", "__version__")):
    try:
        out[mod] = getattr(__import__(mod), attr)
    except Exception as e:
        out[mod] = "error: %s" % e
try:
    import torch
    out["torch"] = torch.__version__
    out["cuda"] = bool(torch.cuda.is_available())
    out["gpu"] = torch.cuda.get_device_name(0) if out["cuda"] else None
except ImportError:
    out["torch"] = None
except Exception as e:
    out["torch"] = "error: %s" % e
print("CHECK " + json.dumps(out))
"""


@dataclass
class Step:
    name: str
    argv: list
    env: dict = field(default_factory=dict, repr=False)
    seconds: float | None = None
    ok: bool | None = None
    tail: str = ""                      # the last lines it printed, when it failed or was checked


@dataclass
class Build:
    method: str
    run: str                            # cold or warm
    folder: str
    steps: list = field(default_factory=list)
    check: dict | None = None
    size_mb: float | None = None


# ================================================================ what to build
def wanted(spec: dict | None, torch: bool) -> dict:
    """The Python version and the pins to install, from the farm's environment file."""
    spec = spec or {}
    pk = spec.get("packages") or {}
    pins = [f"{n}=={pk[n]}" for n in ("torch", "torchvision") if torch and isinstance(pk.get(n), str)]
    cuda = S.cuda_of(pk.get("torch")) if torch else None
    return {"python": str(spec.get("python") or "3.12"),
            "torch": pins, "torch_index": f"https://download.pytorch.org/whl/cu{cuda[0]}{cuda[1]}" if cuda else None,
            "small": [f"{n}=={pk[n]}" if isinstance(pk.get(n), str) else n for n in SMALL]}


def env_python(folder: Path) -> Path:
    """An environment's Python, wherever its layout puts it (venv on Linux or Windows, conda)."""
    for rel in ("bin/python", "Scripts/python.exe", "python.exe", "bin/python3"):
        if (folder / rel).exists():
            return folder / rel
    return folder / ("Scripts/python.exe" if WINDOWS else "bin/python")


def find_conda(agent_python: str | None) -> list | None:
    """The installer's conda, from the agent's Python (…/miniconda3/envs/comfyfarm/bin/python)."""
    for start in filter(None, [agent_python, sys.executable]):
        for parent in Path(start).resolve().parents:
            exe = parent / ("Scripts/conda.exe" if WINDOWS else "bin/conda")
            if parent.name.lower().startswith(("miniconda", "anaconda", "miniforge")) and exe.exists():
                return [str(exe)]
    found = shutil.which("conda")
    return [found] if found else None


def plan(method: str, run: str, work: Path, want: dict, tools: dict) -> Build:
    """The steps of one build: pure, so the tests can read them."""
    folder = work / f"{method}-{run}"
    b = Build(method, run, str(folder))
    py = PY
    if method == "uv":
        uv = tools["uv"]
        env = {"UV_CACHE_DIR": str(work / "uv-cache"), "UV_PYTHON_INSTALL_DIR": str(work / "uv-python"),
               "UV_PYTHON_PREFERENCE": "only-managed",      # its own Python, not this machine's
               "UV_NO_CONFIG": "1"}                         # no uv.toml from anywhere but this test
        b.steps.append(Step("python", uv + ["python", "install", want["python"]], env))
        b.steps.append(Step("venv", uv + ["venv", "--python", want["python"], str(folder)], env))
        if want["torch"]:
            b.steps.append(Step("torch", uv + ["pip", "install", "--python", py, *want["torch"],
                                               "--index-url", want["torch_index"]], env))
        b.steps.append(Step("packages", uv + ["pip", "install", "--python", py, "--index-url", PYPI,
                                              *want["small"]], env))
    else:
        env = {"PIP_CACHE_DIR": str(work / "pip-cache"), "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
        if tools.get("conda"):
            b.steps.append(Step("python", tools["conda"] + ["create", "-y", "-q", "-p", str(folder),
                                                            "--override-channels", "-c", "conda-forge",
                                                            f"python={want['python']}"], env))
        else:
            b.steps.append(Step("python", [tools["base_python"], "-m", "venv", str(folder)], env))
        if want["torch"]:
            b.steps.append(Step("torch", [py, "-m", "pip", "install", *want["torch"],
                                          "--index-url", want["torch_index"]], env))
        b.steps.append(Step("packages", [py, "-m", "pip", "install", "--prefer-binary", "--index-url", PYPI,
                                         *want["small"]], env))
    b.steps.append(Step("check", [py, "-c", CHECK], {}))
    return b


# ================================================================ running it
def execute(step: Step, timeout: float = TIMEOUT) -> None:
    env = {**clean_env(), **step.env}   # never the pipeline's Python settings (CLAUDE.md)
    start = time.monotonic()
    try:
        r = subprocess.run([str(a) for a in step.argv], env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        out, step.ok = (r.stdout or "") + (r.stderr or ""), r.returncode == 0
    except FileNotFoundError:
        out, step.ok = f"{step.argv[0]} isn't installed (or isn't on PATH)", False
    except subprocess.TimeoutExpired:
        out, step.ok = f"took over {timeout:.0f} s", False
    step.seconds = round(time.monotonic() - start, 1)
    step.tail = "\n".join(out.strip().splitlines()[-15:])


def size_mb(folder: Path, seen: set | None = None) -> float:
    """Disk a folder takes, counting a file hard-linked several times once (uv links from its
    cache, so `seen` shared across folders gives what they take together)."""
    seen = set() if seen is None else seen
    total = 0
    for root, _, files in os.walk(folder):
        for f in files:
            try:
                st = os.lstat(os.path.join(root, f))
            except OSError:
                continue
            key = (st.st_dev, st.st_ino) if st.st_ino else (root, f)
            if key not in seen:
                seen.add(key)
                total += st.st_size
    return round(total / 2**20, 1)


def run_build(b: Build, timeout: float = TIMEOUT) -> Build:
    for step in b.steps:
        step.argv = [str(env_python(Path(b.folder))) if a == PY else a for a in step.argv]
        execute(step, timeout)
        if step.name == "check" and step.ok:
            line = next((x for x in step.tail.splitlines() if x.startswith("CHECK ")), None)
            b.check = json.loads(line[6:]) if line else None
        if not step.ok:
            break
    if Path(b.folder).exists():
        b.size_mb = size_mb(Path(b.folder))
    return b


def verdict(b: Build, want: dict) -> list[str]:
    """What is wrong with a build, for the report: empty when it passes."""
    why = [f"step {s.name} failed" for s in b.steps if s.ok is False]
    c = b.check
    if not why and c is None:
        why.append("the check printed nothing")
    if c:
        if not c["python"].startswith(want["python"] + ".") and c["python"] != want["python"]:
            why.append(f"Python {c['python']}, not {want['python']}")
        if want["torch"]:
            pin = want["torch"][0].split("==", 1)[1]
            if c.get("torch") != pin:
                why.append(f"torch {c.get('torch')}, not {pin}")
            elif not c.get("cuda"):
                why.append("torch doesn't see the GPU")
        for mod in ("numpy", "PIL"):
            if str(c.get(mod, "")).startswith("error"):
                why.append(f"{mod}: {c[mod]}")
        if c.get("foreign_paths"):
            why.append("paths from outside the environment: " + ", ".join(c["foreign_paths"]))
    return why


def machine_facts(base_python: str) -> dict:
    facts = {"machine": socket.gethostname(), "os": platform.platform(), "python": sys.version.split()[0]}
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"],
                           capture_output=True, text=True, timeout=30)
        facts["gpus"] = [line.strip() for line in r.stdout.splitlines() if line.strip()]
    except (OSError, subprocess.SubprocessError):
        facts["gpus"] = []
    # Which pip and uv settings this machine has (names only: the studio mirror's address stays out).
    facts["settings_in_environment"] = sorted(k for k in os.environ if k.startswith(("PIP_", "UV_", "CONDA_", "REZ_"))
                                              or k in ("PYTHONPATH", "PYTHONHOME"))
    try:
        r = subprocess.run([base_python, "-m", "pip", "config", "list"], capture_output=True, text=True, timeout=60,
                           env=clean_env())
        facts["pip_config"] = sorted({line.split("=", 1)[0] for line in r.stdout.splitlines() if "=" in line})
    except (OSError, subprocess.SubprocessError):
        facts["pip_config"] = []
    return facts


def get_uv(work: Path, base_python: str, timeout: float) -> tuple[list | None, Step]:
    """uv, installed with pip into a folder of the test's own (no admin, nothing on PATH)."""
    tool = work / "uv-tool"
    venv = Step("get uv", [base_python, "-m", "venv", str(tool)])
    execute(venv, timeout)
    if not venv.ok:
        return None, venv
    pip = Step("get uv", [str(env_python(tool)), "-m", "pip", "install", "--index-url", PYPI, "uv"],
               {"PIP_DISABLE_PIP_VERSION_CHECK": "1"})
    execute(pip, timeout)
    pip.seconds = round((venv.seconds or 0) + (pip.seconds or 0), 1)
    exe = tool / ("Scripts/uv.exe" if WINDOWS else "bin/uv")
    return ([str(exe)] if pip.ok and exe.exists() else None), pip


def summary(report: dict) -> str:
    lines = [f"Builder test on {report['facts']['machine']} ({report['facts']['os']})", ""]
    for m in report["methods"]:
        r = report["methods"][m]
        head = f"{m}: {'PASS' if r['pass'] else 'FAIL'}"
        if r.get("tool"):
            head += f"  ({r['tool']})"
        lines.append(head)
        for why in r.get("problems") or []:
            lines.append(f"        - {why}")
        for b in r["builds"]:
            steps = ", ".join(f"{s['name']} {s['seconds']}s" + ("" if s["ok"] else " FAILED") for s in b["steps"])
            total = sum(s["seconds"] or 0 for s in b["steps"])
            lines.append(f"  {b['run']:4}  {total:7.1f}s  {b['size_mb'] or 0:8.1f} MB   {steps}")
            for why in b["problems"]:
                lines.append(f"        - {why}")
        if r.get("disk_mb") is not None:
            lines.append(f"  both environments and the cache together: {r['disk_mb']:.1f} MB")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--method", choices=(*METHODS, "both"), default="both")
    ap.add_argument("--no-torch", action="store_true", help="skip torch: a quick run")
    ap.add_argument("--work", help="the folder to build in (default run/builder-test in this checkout)")
    ap.add_argument("--uv", nargs="+", help="a uv to use instead of installing one with pip")
    ap.add_argument("--conda", nargs="+", help="the installer's conda (Linux; found from the agent's Python)")
    ap.add_argument("--base-python", default=getattr(sys, "_base_executable", None) or sys.executable,
                    help="the Python that makes the venvs (Windows' installer method) and installs uv")
    ap.add_argument("--keep", action="store_true", help="keep what was built")
    ap.add_argument("--timeout", type=float, default=TIMEOUT, help="seconds a step may take")
    args = ap.parse_args(argv)

    work = Path(args.work) if args.work else ROOT / "run" / "builder-test"
    for m in METHODS:
        shutil.rmtree(work / f"{m}-cold", ignore_errors=True)
        shutil.rmtree(work / f"{m}-warm", ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    want = wanted(S.load(), torch=not args.no_torch)
    agent_python = (ROOT / "run" / "python").read_text(encoding="utf-8").strip() \
        if (ROOT / "run" / "python").exists() else None
    report = {"facts": machine_facts(args.base_python), "wanted": want, "methods": {},
              "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    methods = METHODS if args.method == "both" else (args.method,)
    for m in methods:
        print(f"\n==> {m}", flush=True)
        tools = {"base_python": args.base_python}
        result: dict = {"builds": [], "tool": None}
        if m == "uv":
            uv, got = (args.uv, None) if args.uv else get_uv(work, args.base_python, args.timeout)
            if not uv:
                result.update(problems=[f"no uv: {got.tail if got else 'not found'}"], **{"pass": False})
                report["methods"][m] = result
                continue
            tools["uv"] = uv
            ver = Step("version", uv + ["--version"])
            execute(ver, 60)
            result["tool"] = ver.tail.strip() or "uv"
            if got:
                result["get_uv_seconds"] = got.seconds
        else:                           # Linux's installer makes conda envs; Windows' a venv of python.org's Python
            tools["conda"] = args.conda or (None if WINDOWS else find_conda(agent_python))
            result["tool"] = " ".join(["conda", *tools["conda"][-1:]]) if tools["conda"] \
                else f"venv of {args.base_python}"
        for run in ("cold", "warm"):
            print(f"    {run} ...", flush=True)
            b = run_build(plan(m, run, work, want, tools), args.timeout)
            d = asdict(b)
            d["steps"] = [s for s in d["steps"] if s["ok"] is not None]     # those after a failed one didn't run
            for s in d["steps"]:
                s.pop("env", None)
                s.pop("argv", None)
                if s["ok"] and s["name"] != "check":
                    s["tail"] = ""
            d["problems"] = verdict(b, want)
            result["builds"].append(d)
        # Both environments and the method's caches together, a hard-linked file counted once.
        seen: set = set()
        parts = [work / f"{m}-cold", work / f"{m}-warm",
                 *[work / c for c in (("uv-cache", "uv-python") if m == "uv" else ("pip-cache",))]]
        result["disk_mb"] = round(sum(size_mb(p, seen) for p in parts if p.exists()), 1)
        result["pass"] = all(not b["problems"] for b in result["builds"]) and len(result["builds"]) == 2
        report["methods"][m] = result
    out = work / f"{report['facts']['machine']}.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("\n" + summary(report))
    print(f"Report: {out}\nCommit it to docs/builder/ (see docs/builder/README.md).")
    if not args.keep:
        for m in METHODS:
            for run in ("cold", "warm"):
                shutil.rmtree(work / f"{m}-{run}", ignore_errors=True)
        for c in ("uv-cache", "uv-python", "uv-tool", "pip-cache"):
            shutil.rmtree(work / c, ignore_errors=True)
    return 0 if all(r["pass"] for r in report["methods"].values()) else 1


if __name__ == "__main__":
    sys.exit(main())
