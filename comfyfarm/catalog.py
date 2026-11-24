"""The catalogue: where each definition sits in every menu (decision 0054).

Every client draws its menus Provider > Task > Workflow, for example ComfyUI Local >
Masking > SAM3. The two lists the menus are made of live in the reviewed repo:

- providers/<name>.yaml: what an artist picks first. A provider is a runner type on a
  target (pool, later the gateway or a cloud), with its label in the menus and whether a
  job sends material outside Rodeo (off-site). Today: comfyui-local and python-local.
- workflows/tasks.yaml: what the artist wants done (Masking, Video ...), in menu order.

A definition names its task (`task: masking`), and its provider only when it isn't the
usual one of its runner type (DEFAULT_PROVIDER). `family:` names the same model on several
providers (their prices are compared later); `hidden: true` keeps a definition out of
artists' menus (gpu_test), while the dashboard, the CLI and evals still run it.

None of this changes what a job runs, so it isn't part of a definition's fingerprint
(definitions.py): moving a workflow to another task never makes a machine refuse a job.

definitions.py checks each definition's task and provider against the catalogue
(`Catalog.problems`), and the manager draws the menus from it for a show
(`Catalog.tree`, GET /api/v1/catalog). The catalogue is the checkout's, read again when its
files change (`current`). It needs the standard library and yaml only."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .fileio.ocio import REPO

__all__ = ["Catalog", "Provider", "Task", "DEFAULT_PROVIDER", "load", "current", "NAME", "FAMILY"]

PROVIDERS = "providers"                 # one file per provider, under the checkout
TASKS = "workflows/tasks.yaml"
NAME = re.compile(r"[a-z][a-z0-9-]{0,31}")              # a provider's or a task's name
FAMILY = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
PROVIDER_KEYS = ("label", "description", "runner", "target", "offsite", "order", "adapter", "lanes", "keys")
# An outside provider's (runner api, target gateway; decision 0055): the gateway's code that
# speaks to it (runners/api), how many of its jobs a gateway runs at once unless gateway.yaml
# says, and the names of the keys gateway.yaml holds for it (never the keys).
API_PROVIDER_KEYS = ("adapter", "lanes", "keys")
TASK_KEYS = ("label", "description")
# The provider of a definition that names none: its runner type's, on the farm's own GPUs.
DEFAULT_PROVIDER = {"comfyui": "comfyui-local", "process": "python-local"}


@dataclass(frozen=True)
class Provider:
    name: str
    label: str
    runner: str                         # the runner type its definitions have (runner.type)
    target: str                         # where its jobs are sent (definitions.TARGETS)
    offsite: bool = False               # its jobs send material outside Rodeo's network
    order: int = 100                    # its place in the menus, smallest first
    description: str = ""
    adapter: str = ""                   # an API provider's: runners/api/<adapter>.py
    lanes: int = 0                      # an API provider's jobs at once on a gateway, unless gateway.yaml says
    keys: tuple = ()                    # the names of the keys gateway.yaml holds for it

    def as_dict(self) -> dict:
        return {"name": self.name, "label": self.label, "runner": self.runner, "target": self.target,
                "offsite": self.offsite, "description": self.description}


@dataclass(frozen=True)
class Task:
    name: str
    label: str
    description: str = ""

    def as_dict(self) -> dict:
        return {"name": self.name, "label": self.label, "description": self.description}


@dataclass
class Catalog:
    root: Path
    providers: dict[str, Provider] = field(default_factory=dict)     # in menu order
    tasks: dict[str, Task] = field(default_factory=dict)             # in menu order
    errors: list[str] = field(default_factory=list)                  # what is wrong with its files

    def problems(self, task: str | None, provider: str | None, runner_type: str, named: bool) -> list[str]:
        """What is wrong with a definition's place in the menus: its task (None: it names
        none) and its provider (`named`: the definition names it, else its runner type's).
        Nothing when the catalogue's own files are broken: that is said once, for the
        catalogue (`comfyfarm check`, the manager's log), not on every definition."""
        if self.errors:
            return []
        out = []
        if task is not None and task not in self.tasks:
            out.append(f"task: {task} isn't in {TASKS} (it has {', '.join(self.tasks)})")
        if provider is None:
            return out
        p = self.providers.get(provider)
        if p is None:
            out.append(f"provider: {provider} isn't in {PROVIDERS}/ (it has {', '.join(self.providers)})" if named
                       else f"runner.type {runner_type} has no provider: {PROVIDERS}/{provider}.yaml is missing")
        elif p.runner != runner_type:
            out.append(f"provider: {provider} runs runner.type {p.runner} definitions, and this one is "
                       f"runner.type {runner_type}")
        return out

    def place(self, task: str | None, provider: str | None, hidden: bool = False) -> str:
        """Where a definition shows in the menus, as people read it: ComfyUI Local > Masking."""
        p = self.providers.get(provider or "")
        t = self.tasks.get(task or "")
        where = f"{p.label if p else provider or '?'} > {t.label if t else task or 'no task'}"
        return f"{where} (hidden from artists)" if hidden else where

    def summary(self) -> str:
        return (f"{len(self.providers)} provider(s) ({', '.join(p.label for p in self.providers.values())}), "
                f"{len(self.tasks)} task(s) ({', '.join(t.label for t in self.tasks.values())})")

    def tree(self, definitions: Iterable, refusal: Callable | None = None, hidden: bool = False) -> list[dict]:
        """The menus, as every client draws them (GET /api/v1/catalog): providers in their
        order, each with its tasks in tasks.yaml's order, each with its workflows by label.

        - `definitions`: the loaded definitions (a DefinitionRegistry's).
        - `refusal(definition)`: why the show may not use it, as (reason, policy rule), or
          None. A refused workflow stays listed, so a menu can say why (Nuke: "not on this
          show").
        - `hidden`: list the hidden ones too, marked (the dashboard). Artists' menus don't.

        A provider or a task with nothing in it isn't listed. A definition with no task is
        under "Other", after its provider's tasks. One whose provider or task the catalogue
        doesn't have (it is turned off, or the catalogue's files are broken) is still listed,
        under that name, after the known ones, so a broken file never empties a menu."""
        shown = [wf for wf in definitions if hidden or wf.hidden is not True]
        family: dict[str, list] = {}
        for wf in shown:
            if isinstance(wf.family, str):
                family.setdefault(wf.family, []).append(wf)
        groups: dict[str, dict] = {}
        for wf in shown:
            p = wf.provider if isinstance(wf.provider, str) else "?"
            t = wf.task if isinstance(wf.task, str) else None
            groups.setdefault(p, {}).setdefault(t, []).append(wf)

        def label_of(provider) -> str:
            p = self.providers.get(provider or "")
            return p.label if p else str(provider)

        def entry(wf) -> dict:
            why = refusal(wf) if refusal is not None else None
            p = self.providers.get(wf.provider if isinstance(wf.provider, str) else "")
            return {
                "name": wf.name, "label": wf.label or wf.name, "description": wf.description,
                "task": wf.task, "provider": wf.provider, "family": wf.family,
                "offsite": p.offsite if p else False, "hidden": wf.hidden is True,
                "enabled": wf.enabled, "problems": list(wf.problems),
                "allowed": why is None, "reason": why[0] if why else None, "rule": why[1] if why else None,
                "siblings": [{"name": s.name, "label": s.label or s.name, "provider": s.provider,
                              "provider_label": label_of(s.provider)}
                             for s in family.get(wf.family, []) if s is not wf] if isinstance(wf.family, str) else [],
            }

        def known_first(names, known) -> list:
            return [n for n in known if n in names] + sorted(n for n in names if n not in known and n is not None)

        out = []
        for pname in known_first(groups, self.providers):
            p = self.providers.get(pname)
            tasks = []
            by_task = groups[pname]
            for tname in [*known_first(by_task, self.tasks), *([None] if None in by_task else [])]:
                t = self.tasks.get(tname or "")
                head = t.as_dict() if t else {"name": tname, "label": tname or "Other", "description": ""}
                workflows = sorted(by_task[tname], key=lambda w: ((w.label or w.name).lower(), w.name))
                tasks.append({**head, "workflows": [entry(wf) for wf in workflows]})
            head = p.as_dict() if p else {"name": pname, "label": pname, "runner": None, "target": None,
                                          "offsite": False, "description": ""}
            out.append({**head, "tasks": tasks})
        return out


def _provider(path: Path) -> Provider:
    from .definitions import RUNNER_TYPES, TARGETS
    name = path.stem
    if not NAME.fullmatch(name):
        raise ValueError(f"{name} isn't a provider's name: lower-case letters, digits and dashes, starting with a "
                         f"letter (comfyui-local)")
    d = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(d, dict):
        raise ValueError("needs to be a section like {label: ComfyUI Local, runner: comfyui, target: pool}")
    unknown = sorted(set(d) - set(PROVIDER_KEYS))
    if unknown:
        raise ValueError(f"unknown key(s) {', '.join(map(str, unknown))} (a provider has {', '.join(PROVIDER_KEYS)})")
    label, runner, target = d.get("label"), d.get("runner"), d.get("target")
    if not (isinstance(label, str) and label.strip()):
        raise ValueError("label: needs to be its name in the menus, e.g. ComfyUI Local")
    if runner not in RUNNER_TYPES:
        raise ValueError(f"runner: needs to be the runner type its definitions have ({', '.join(RUNNER_TYPES)})")
    if target not in TARGETS:
        raise ValueError(f"target: needs to be where its jobs go ({', '.join(TARGETS)})")
    if (runner == "api") != (target == "gateway"):
        raise ValueError("target: an API provider's jobs go to the gateway (runner: api, target: gateway), and no "
                         "other provider's do")
    extra = sorted(k for k in API_PROVIDER_KEYS if k in d)
    if runner != "api" and extra:
        raise ValueError(f"{extra[0]}: only an API provider (runner: api) has {', '.join(API_PROVIDER_KEYS)}")
    adapter, lanes, keys = d.get("adapter", ""), d.get("lanes", 1), d.get("keys") or []
    if runner == "api":
        if not (isinstance(adapter, str) and NAME.fullmatch(adapter)):
            raise ValueError("adapter: needs to be the name of the gateway's code that speaks to it, e.g. fal")
        if isinstance(lanes, bool) or not isinstance(lanes, int) or lanes < 1:
            raise ValueError("lanes: needs to be a whole number, 1 or more: its jobs at once on a gateway")
        if not (isinstance(keys, list) and all(isinstance(k, str) and NAME.fullmatch(k) for k in keys)
                and len(set(keys)) == len(keys)):
            raise ValueError("keys: needs to be a list of names for the keys gateway.yaml holds, e.g. [requests, "
                             "billing] (never the keys themselves)")
    offsite, order, description = d.get("offsite", False), d.get("order", 100), d.get("description") or ""
    if not isinstance(offsite, bool):
        raise ValueError("offsite: needs to be true (its jobs send material outside Rodeo) or false")
    if isinstance(order, bool) or not isinstance(order, int):
        raise ValueError("order: needs to be a whole number, its place in the menus (smallest first)")
    if not isinstance(description, str):
        raise ValueError("description: needs to be text")
    return Provider(name=name, label=label.strip(), runner=runner, target=target, offsite=offsite, order=order,
                    description=description.strip(), adapter=adapter if runner == "api" else "",
                    lanes=lanes if runner == "api" else 0, keys=tuple(keys) if runner == "api" else ())


def _tasks(path: Path) -> dict[str, Task]:
    d = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(d, dict) or not d:
        raise ValueError("needs to be the tasks in menu order, e.g. masking: {label: Masking}")
    out: dict[str, Task] = {}
    for name, spec in d.items():
        if not (isinstance(name, str) and NAME.fullmatch(name)):
            raise ValueError(f"{name} isn't a task's name: lower-case letters, digits and dashes, starting with a "
                             f"letter (masking)")
        if not isinstance(spec, dict) or set(spec) - set(TASK_KEYS):
            raise ValueError(f"{name}: needs to be a section with {' and '.join(TASK_KEYS)}, e.g. {{label: Masking}}")
        label, description = spec.get("label"), spec.get("description") or ""
        if not (isinstance(label, str) and label.strip()):
            raise ValueError(f"{name}.label: needs to be its name in the menus, e.g. Masking")
        if not isinstance(description, str):
            raise ValueError(f"{name}.description: needs to be text")
        out[name] = Task(name=name, label=label.strip(), description=description.strip())
    return out


def _twice(labels: list[str], what: str) -> list[str]:
    seen, out = set(), []
    for label in labels:
        if label.lower() in seen:
            out.append(f"two {what} are labelled {label}: the menus couldn't tell them apart")
        seen.add(label.lower())
    return out


def load(root: Path = REPO) -> Catalog:
    """The catalogue of a checkout: its providers and tasks in menu order, and what is wrong
    with their files."""
    cat = Catalog(root=root)
    folder = root / PROVIDERS
    providers = []
    if not folder.is_dir():
        cat.errors.append(f"{PROVIDERS}/ isn't in {root}: the menus have no providers")
    else:
        for f in sorted(folder.glob("*.yaml")):
            try:
                providers.append(_provider(f))
            except (OSError, yaml.YAMLError, ValueError) as e:
                cat.errors.append(f"{PROVIDERS}/{f.name}: {e}")
    providers.sort(key=lambda p: (p.order, p.name))
    cat.providers = {p.name: p for p in providers}
    try:
        cat.tasks = _tasks(root / TASKS)
    except FileNotFoundError:
        cat.errors.append(f"{TASKS} isn't in {root}: the menus have no tasks")
    except (OSError, yaml.YAMLError, ValueError) as e:
        cat.errors.append(f"{TASKS}: {e}")
    if folder.is_dir():
        cat.errors += [f"{PROVIDERS}/{name}.yaml is missing: it is the provider of runner.type {runner} definitions"
                       for runner, name in DEFAULT_PROVIDER.items() if name not in cat.providers
                       and not any(e.startswith(f"{PROVIDERS}/{name}.yaml") for e in cat.errors)]
    cat.errors += _twice([p.label for p in providers], "providers") + _twice(
        [t.label for t in cat.tasks.values()], "tasks")
    return cat


_cache: dict[Path, tuple[tuple, Catalog]] = {}


def _signature(root: Path) -> tuple:
    files = [*sorted((root / PROVIDERS).glob("*.yaml")), root / TASKS]
    out = []
    for f in files:
        try:
            st = f.stat()
            out.append((f.name, st.st_mtime_ns, st.st_size))
        except OSError:
            out.append((f.name, None, None))
    return tuple(out)


def current(root: Path = REPO) -> Catalog:
    """The checkout's catalogue, read again when one of its files changes."""
    sig = _signature(root)
    hit = _cache.get(root)
    if hit is None or hit[0] != sig:
        hit = (sig, load(root))
        _cache[root] = hit
    return hit[1]
