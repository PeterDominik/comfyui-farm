"""A frame sequence (writes: frames): the frames a run writes, renamed to the shot's frame
numbers in a versioned output folder, with a preview movie of them (decision 0034).

The manager picks the folder (paths.output_dir, the first version no file or queued job
has) and the file names (paths.frame_name), frozen per job. The runner writes its frames
there as <frames_prefix>_<number>.<ext>; the agent renames them, numbered from the first
input frame, and then either copies the video the run made itself (writes.preview: own)
or makes the preview movie of the frames (fileio.preview)."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from ..config import PathsConfig
from ..fileio import naming as N
from ..fileio import outputs as O
from ..fileio import paths as P
from ..fileio import preview as PV
from .base import PlanContext, PlanError, RunContext

log = logging.getLogger("comfyfarm")

_DEFAULT_PATHS = PathsConfig()      # names for jobs queued before they were frozen per job


class Frames:
    kind = "frames"

    def writes(self, wf) -> bool:
        return bool(wf.frames_ext) and not wf.textures

    def plan(self, plan: dict, ctx: PlanContext) -> dict:
        """The versioned folder the frames go into and their names, from the first input frame."""
        pc, wf, values, seq = ctx.cfg.paths, plan["wf"], plan["values"], plan["seq"]
        show_, shot_, seq_name, source = plan["show"], plan["shot"], plan["seq_name"], plan["source"]
        job_id = ctx.job_id
        # What the run's frames and its own video start with, until they're renamed.
        plan["frames_prefix"] = f"_comfyfarm_{job_id[:8]}_raw"
        plan["preview_prefix"] = f"comfyfarm/{job_id[:8]}_{source}_{wf.tag}"
        picked = values.get(wf.output_folder_param) if wf.output_folder_param else None
        root = N.resolve_output_folder(picked, pc) if picked else N.output_root(pc, show_, seq_name, shot_)
        dir_template = pc.output_dir
        if picked and not shot_ and "{shot" in dir_template:
            dir_template = "{root}"
            plan["notes"].append("No shot in the input path, so the frames go straight into the output folder.")
        versioned = "{version" in pc.frame_name or "{version" in dir_template
        exists = N.sequence_exists if pc.validate_inputs else (lambda _d, _p: False)
        base = {"source": source, "show": show_, "seq": seq_name, "shot": shot_, "tag": wf.tag,
                "ext": wf.frames_ext}
        for v in range(1, 1000):
            out_dir = N.output_dir(dir_template, root=root, version=v, **base)
            tokens = dict(base, version=v, folder=out_dir.rpartition("/")[2])
            pattern = P.render(P.frame_pattern(pc.frame_name), "file name", **tokens)
            key = f"{out_dir}/{pattern}"
            # Taken = files of this version already there, or promised to a queued job.
            if not (ctx.reserved(key) or exists(out_dir, pattern)):
                break
            if not versioned:
                raise PlanError(f"{P.to_posix(key, pc.mappings) or key} already exists. Add {{version}} "
                                f"to paths.frame_name so new runs don't overwrite it.", 409)
        else:
            raise PlanError(f"No free version in {out_dir} (999 tried).")
        P.check_writable(out_dir, pc)
        plan.update(out_dir=out_dir, version=v, picked=bool(picked), output_key=key)
        plan["name_tokens"] = tokens
        first = seq.first if seq and seq.first is not None else 1
        count = seq.frame_count if seq else None
        plan["first_frame"] = first
        plan["first_file"] = P.render(pc.frame_name, "file name", frame=first, **tokens)
        plan["last_file"] = P.render(pc.frame_name, "file name", frame=first + count - 1, **tokens) if count else None
        return plan

    def info(self, plan: dict, ctx: PlanContext) -> dict:
        pc = ctx.cfg.paths
        return {
            "frames_prefix": plan["frames_prefix"], "preview_prefix": plan["preview_prefix"],
            # Frozen per job, so a config edit can't rename frames of jobs already queued.
            "name_tokens": plan.get("name_tokens"), "frame_name": pc.frame_name,
            "preview_name": pc.preview_name,
            # What the agent collects when the run is done, and the preview movie it makes of
            # the frames (fileio.preview). No node ids: the agent's runner has its own copy.
            "collect": plan["wf"].collect(self._movie(plan["wf"], plan["seq"], ctx.cfg.preview)),
        }

    @staticmethod
    def _movie(wf, seq, pv) -> dict | None:
        """The preview movie's settings for a job (None: no movie): preview.*, at the
        input movie's frame rate when the input is one."""
        if not pv.enabled or not wf.makes_preview:
            return None
        c = (seq.convert if seq else None) or {}
        fps = c.get("fps") if c.get("kind") == "movie" and c.get("fps") else pv.fps
        fps_str = c.get("fps_str") if c.get("kind") == "movie" and c.get("fps_str") else PV.rate_text(pv.fps)
        return {"fps": float(fps), "fps_str": fps_str, "max_width": pv.max_width, "crf": pv.crf}

    def request(self, plan: dict) -> dict:
        return {"frames_prefix": plan["frames_prefix"], "preview_prefix": plan["preview_prefix"]}

    def summary(self, plan: dict) -> dict:
        return {}

    def collects(self, job: dict) -> bool:
        c = job.get("collect") or {}
        return bool(c.get("frames") or c.get("preview") or c.get("movie"))

    async def prepare(self, ctx: RunContext) -> None:
        pass                            # the runner writes straight into the output folder

    async def collect(self, ctx: RunContext) -> tuple[dict, list[str]]:
        info, collect = ctx.info, ctx.job.get("collect") or {}
        mappings = ctx.cfg.paths.mappings
        out_dir = info.get("output_dir")
        outputs: dict = {}
        warnings: list[str] = []
        tokens = dict(info.get("name_tokens") or {})

        fr = collect.get("frames")
        if fr and out_dir:
            res = await asyncio.to_thread(O.rename_frames, info, fr.get("ext", "png"), _DEFAULT_PATHS.frame_name,
                                          mappings)
            outputs.update(res)
            exp = info.get("expected_frames")
            if exp and res["frames"] != exp:
                warnings.append(f"Got {res['frames']} output frames from {exp} input frames; "
                                f"numbering starts at {res['first']} and may not line up with the plate.")

        if collect.get("preview"):         # True; a job queued before agents found their nodes has the section
            entries = ctx.runner.preview(ctx.run, ctx.result)
            if entries and out_dir:
                e = entries[-1]
                dest = Path(out_dir) / (info.get("preview_name") or _DEFAULT_PATHS.preview_name).format(**tokens)
                try:
                    await ctx.runner.fetch(e, dest)
                    O.readable(str(dest))
                    outputs["preview"] = P.normalize(str(dest))
                    outputs["preview_posix"] = P.to_posix(outputs["preview"], mappings)
                except Exception as ex:  # noqa: BLE001
                    warnings.append(f"Couldn't copy the preview video: {ex}")
            elif not entries:
                warnings.append(f"{ctx.runner.label} returned no preview video.")

        # The preview movie of the frames (fileio.preview). Not for one frame, and never a
        # reason to fail the job.
        movie = collect.get("movie")
        if movie and out_dir and "preview" not in outputs and (outputs.get("frames") or 0) >= 2:
            await ctx.report("making the preview movie")
            dest = str(Path(out_dir) / (info.get("preview_name") or _DEFAULT_PATHS.preview_name).format(**tokens))
            try:
                await PV.make_movie(outputs, movie, dest, ctx.cfg.convert)
                O.readable(dest)
                outputs["preview"] = P.normalize(dest)
                outputs["preview_posix"] = P.to_posix(outputs["preview"], mappings)
            except Exception as ex:  # noqa: BLE001
                log.warning("preview movie for %s: %s", ctx.run.id[:8], ex)
                warnings.append(f"Couldn't make the preview movie: {ex}")
        return outputs, warnings


HANDLER = Frames()
