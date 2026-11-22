"""The seam between the farm and a kind of output it writes (decision 0034).

A handler is one kind of artifact: what a definition says it writes under the head's
`writes:` (frames, textures, file). It has a half for each side of the farm:

  the manager's   writes(wf) says whether a definition writes this kind; plan() works out,
                  when a job is planned, where its files go and what they are called (an
                  output folder with a free version), into the plan the job is made from;
                  from that plan, info() is what the job keeps of it (frozen at submit, for
                  the agent), request() what the runner is told, summary() what the
                  dashboard shows before submitting
  the agent's     collects(job) says whether a job writes this kind, from the job alone;
                  prepare() runs before the run (folders the runner writes into), and
                  collect() after it: what the runner wrote turned into the farm's files,
                  as the outputs the manager keeps and the warnings for the artist; and
                  record() is its entry in the job's list of artifacts, from those outputs

Handlers import neither the manager nor the agent: what they need comes in a context. The
manager's errors are PlanError (with the HTTP status the manager answers with), the
agent's PrepareError (the job fails with its message).

Handler is a protocol, for the reader and the tests. Generic is what a kind the core
knows nothing about is made from (decision 0036): file, movie, model."""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol

from ..fileio import naming as N
from ..fileio import outputs as O
from ..fileio import paths as P


class PlanError(Exception):
    """Where a job's outputs would go can't be worked out. Said for the artist."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class PrepareError(Exception):
    """A run can't be prepared for this kind of output: the job fails with this message."""


@dataclass
class PlanContext:
    """What the manager gives a handler's plan()."""
    cfg: Any                                # the manager's Config (paths, ocio, preview)
    job_id: str                             # "preview" while the dashboard only looks
    scratch_root: str                       # the agents' scratch folder, as jobs name it
    reserved: Callable[[str], bool]         # files promised to a job still queued or running


@dataclass
class RunContext:
    """What the agent gives a handler before and after a run."""
    job: dict                               # the job as the agent holds it: info (frozen at submit), collect
    cfg: Any                                # the agent's AgentConfig
    run: Any = None                         # the run (agent.Held): its id
    runner: Any = None                      # the runner that ran it: its own preview video (preview, fetch)
    result: Any = None                      # what the runner returned when the run ended
    report: Callable[[str], Awaitable[None]] | None = None   # a postprocessing stage, for the dashboard
    spawn: Callable[[Awaitable], None] | None = None          # a report sent without waiting for it

    @property
    def info(self) -> dict:
        return self.job.get("info") or {}


class Handler(Protocol):
    kind: str                               # its key under the head's writes:, and its module's name

    def writes(self, wf) -> bool: ...                   # the definition writes this kind (the manager)
    def plan(self, plan: dict, ctx: PlanContext) -> dict: ...    # where it goes; PlanError if it can't
    def info(self, plan: dict, ctx: PlanContext) -> dict: ...    # kept with the job, for the agent
    def request(self, plan: dict) -> dict: ...          # told to the runner: where it writes, what it starts with
    def summary(self, plan: dict) -> dict: ...          # shown before submitting
    def collects(self, job: dict) -> bool: ...          # the job writes this kind (the agent)
    async def prepare(self, ctx: RunContext) -> None: ...        # before the run; PrepareError
    async def collect(self, ctx: RunContext) -> tuple[dict, list[str]]: ...   # after: outputs, warnings
    def record(self, outputs: dict) -> dict | None: ...  # its entry in the job's artifacts, from collect's outputs


EXT = re.compile(r"^[A-Za-z0-9]{1,12}$")
SEPARATOR = re.compile(r"[._-]?\{frame[^}]*\}")


def base_name(frame_name: str, tokens: dict) -> str:
    """A job's name for its files besides frames: paths.frame_name up to its frame number,
    so it follows the farm's naming ({source}.{tag}_v{version:03d}.{frame:04d}.{ext} gives
    plate.gputest_v001)."""
    template = SEPARATOR.split(frame_name)[0] if SEPARATOR.search(frame_name) else \
        re.sub(r"\.?\{ext[^}]*\}$", "", frame_name)
    return P.render(template, "file name", **tokens)


class Generic:
    """A kind of output the core knows nothing about (decision 0036). A kind is a subclass
    that names `kind` and, if it takes more than an extension, what writes.<kind> takes.

    The run writes its files named <prefix><anything>: a process into the folder job.json
    gives it (writes.<kind>.dir), a ComfyUI graph through the node its runner block names
    (runner.outputs.<kind>.node), whose files the agent fetches. The agent names them
    <base><anything>, <base> the job's name for the version (base_name): a process writing
    _comfyfarm_1234abcd_file.json gives plate.gputest_v001.json. With frames, the files go
    into the frames' folder with their version; alone, into the first version of the
    output folder no file or queued job has. A run that writes none fails the job: it
    said it writes them."""
    kind = ""
    generic = True
    KEYS = ("ext",)                     # what writes.<kind> takes

    # ------------------------------------------------ the head
    def head(self, section) -> dict:
        """writes.<kind> as the definition says it (ValueError: what is wrong with it)."""
        section = {} if section in (None, True) else section
        if not isinstance(section, dict):
            raise ValueError(f"writes.{self.kind}: needs to be a section, e.g. {{ext: json}}")
        for k in section:
            if k not in self.KEYS:
                raise ValueError(f"writes.{self.kind}: unknown key {k} (it takes {', '.join(self.KEYS)})")
        ext = section.get("ext")
        if ext is not None and not (isinstance(ext, str) and EXT.match(ext)):
            raise ValueError(f"writes.{self.kind}.ext: a file extension without its dot, e.g. json")
        return dict(section)

    # ------------------------------------------------ the manager's half
    def writes(self, wf) -> bool:
        return self.kind in (getattr(wf, "writes", None) or {})

    def plan(self, plan: dict, ctx: PlanContext) -> dict:
        pc, wf = ctx.cfg.paths, plan["wf"]
        ext = (wf.writes.get(self.kind) or {}).get("ext")
        if not plan.get("out_dir"):
            self._folder(plan, ctx, ext)       # it writes no frames: the files choose the folder
        tokens = plan.get("name_tokens") or {"source": plan["source"], "show": plan["show"], "seq": plan["seq_name"],
                                             "shot": plan["shot"], "tag": wf.tag, "version": plan["version"]}
        base = base_name(pc.frame_name, dict(tokens, ext=ext or ""))
        plan.setdefault("writes", {})[self.kind] = {"dir": plan["out_dir"], "prefix": f"_comfyfarm_{ctx.job_id[:8]}_"
                                                    f"{self.kind}", "ext": ext, "base": base}
        return plan

    def _folder(self, plan: dict, ctx: PlanContext, ext: str | None) -> None:
        """The output folder's first version that no file of this name, nor a queued job, has."""
        pc, wf, values = ctx.cfg.paths, plan["wf"], plan["values"]
        picked = values.get(wf.output_folder_param) if wf.output_folder_param else None
        root = N.resolve_output_folder(picked, pc) if picked else \
            N.output_root(pc, plan["show"], plan["seq_name"], plan["shot"])
        dir_template = pc.output_dir
        if picked and not plan["shot"] and "{shot" in dir_template:
            dir_template = "{root}"
            plan["notes"].append("No shot in the input path, so the files go straight into the output folder.")
        versioned = "{version" in pc.frame_name or "{version" in dir_template
        tokens = {"source": plan["source"], "show": plan["show"], "seq": plan["seq_name"], "shot": plan["shot"],
                  "tag": wf.tag, "ext": ext or ""}
        for v in range(1, 1000):
            out_dir = N.output_dir(dir_template, root=root, version=v, **tokens)
            named = dict(tokens, version=v, folder=out_dir.rpartition("/")[2])
            base = base_name(pc.frame_name, named)
            key = f"{out_dir}/{base}"
            if not (ctx.reserved(key) or (pc.validate_inputs and _taken(out_dir, base))):
                break
            if not versioned:
                raise PlanError(f"{P.to_posix(key, pc.mappings) or key} already exists. Add {{version}} "
                                f"to paths.frame_name so new runs don't overwrite it.", 409)
        else:
            raise PlanError(f"No free version in {out_dir} (999 tried).")
        P.check_writable(out_dir, pc)
        plan.update(out_dir=out_dir, version=v, picked=bool(picked), output_key=key, name_tokens=named,
                    first_file=base + (f".{ext}" if ext else ""), last_file=None)

    def info(self, plan: dict, ctx: PlanContext) -> dict:
        return {"writes": {self.kind: dict(plan["writes"][self.kind])}}     # frozen per job: where, named what

    def request(self, plan: dict) -> dict:
        w = plan["writes"][self.kind]
        return {"writes": {self.kind: {"dir": w["dir"], "prefix": w["prefix"], "ext": w["ext"]}}}

    def summary(self, plan: dict) -> dict:
        w = plan["writes"][self.kind]
        return {"artifacts": [{"kind": self.kind, "name": w["base"] + "*" + (f".{w['ext']}" if w["ext"] else "")}]}

    # ------------------------------------------------ the agent's half
    def collects(self, job: dict) -> bool:
        return self.kind in ((job.get("info") or {}).get("writes") or {})

    async def prepare(self, ctx: RunContext) -> None:
        """A first attempt's files are removed, so they can't be taken for this one's."""
        w = ctx.info["writes"][self.kind]
        try:
            await asyncio.to_thread(_clear, w["dir"], w["prefix"])
        except OSError as e:
            raise PrepareError(f"Can't clear {w['dir']} of an earlier attempt's {self.kind} files: "
                               f"{e.strerror or e}") from None

    async def collect(self, ctx: RunContext) -> tuple[dict, list[str]]:
        w = ctx.info["writes"][self.kind]
        mappings = ctx.cfg.paths.mappings
        entries = ctx.runner.files(ctx.run, ctx.result, self.kind, w)
        if not entries:
            raise RuntimeError(f"the run wrote no {self.kind} ({w['prefix']}*" + (f".{w['ext']}" if w["ext"] else "")
                               + f" in {w['dir']}), though its definition says it writes one")
        files = []
        for e in entries:
            name = os.path.basename(e) if isinstance(e, (str, os.PathLike)) else str(e.get("filename") or "")
            rest = name[len(w["prefix"]):] if name.startswith(w["prefix"]) else "_" + name
            dest = os.path.join(w["dir"], w["base"] + rest)
            await ctx.runner.fetch(e, dest)
            O.readable(dest)
            files.append(P.normalize(dest))
        return {self.kind: {"files": files, "files_posix": [P.to_posix(f, mappings) for f in files]}}, []

    def record(self, outputs: dict) -> dict | None:
        return {"kind": self.kind, **outputs[self.kind]} if outputs.get(self.kind) else None


def _clear(folder: str, prefix: str) -> None:
    try:
        names = os.listdir(folder)
    except FileNotFoundError:
        return
    for n in names:
        if n.startswith(prefix):
            os.remove(os.path.join(folder, n))


def _taken(folder: str, base: str) -> bool:
    try:
        names = os.listdir(folder)
    except OSError:
        return False
    return any(n == base or n.startswith((base + ".", base + "_", base + "-")) for n in names)
