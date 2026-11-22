"""A texture set (writes: textures): PBR maps written as EXRs named by the texture
department's TEX convention, in a versioned folder of their own (decision 0034; the
convention is in fileio/textures.py).

The manager works out which UDIM or frame each input texture is, every map's file name,
and the folder (the first version nobody has), frozen per job with the repo's OCIO
config; a texture with more tiles than one run takes becomes a set of batches filling
that folder. The runner writes each map's images into the job's scratch (raw_dir), which
the agent empties before the run; after it, the agent writes each image as its EXR."""

from __future__ import annotations

import asyncio
import os
import shutil

from ..fileio import inputs as I
from ..fileio import naming as N
from ..fileio import ocio as OC
from ..fileio import outputs as O
from ..fileio import paths as P
from ..fileio import textures as T
from ..protocol import now
from .base import PlanContext, PlanError, PrepareError, RunContext


class Textures:
    kind = "textures"

    def writes(self, wf) -> bool:
        return bool(wf.textures)

    def plan(self, plan: dict, ctx: PlanContext) -> dict:
        """Texture workflows: which UDIM/frame each input texture is, the TEX file names of
        every map, and a versioned folder for this run (the names carry no version)."""
        pc, wf, values, seq = ctx.cfg.paths, plan["wf"], plan["values"], plan["seq"]
        job_id = ctx.job_id
        spec = wf.textures
        if seq is None:
            raise PlanError("This workflow needs an input texture.")
        numbering = values.get("numbering") or T.DEFAULTS["numbering"]
        if pc.validate_inputs:
            names = I.loaded_names(seq)
        else:   # can't list the input: go by the frame numbers alone
            nums = seq.frames or (range(seq.first, seq.last + 1) if seq.first is not None else [None])
            names = [f"x.{n:04d}.png" if n is not None else "x.png" for n in nums]
        items, has_udim = T.items_for(names, numbering)
        tokens = T.name_tokens(values, T.resolve_tile(values.get("tile") or "auto", items, has_udim))

        picked = values.get(wf.output_folder_param) if wf.output_folder_param else None
        root = N.resolve_output_folder(picked, pc) if picked else \
            N.output_root(pc, plan["show"], plan["seq_name"], plan["shot"])
        asset = P.context_from_path(seq.farm_path, pc.context_patterns, pc.shot_name).get("asset")
        base = {"root": root, "show": plan["show"], "seq": plan["seq_name"], "shot": plan["shot"],
                "asset": asset, "source": plan["source"], "tag": wf.tag}

        def taken(d: str) -> bool:   # files there already, or promised to a queued job
            if ctx.reserved(d + "/"):
                return True
            try:
                return pc.validate_inputs and bool(os.listdir(d))
            except OSError:
                return False

        for v in range(1, 1000):
            out_dir = T.output_dir(spec.dirs, version=v, **base)
            if not taken(out_dir):
                break
        else:
            raise PlanError(f"No free version for {out_dir} (999 tried).")
        P.check_writable(out_dir, pc)
        maps = [{"name": m.name, "channel": m.channel, "colorspace": m.colorspace,
                 "color": m.color, "bits": m.bits, "prefix": f"_cf{job_id[:8]}_{m.channel}"} for m in spec.maps]
        textures = {"raw_dir": f"{ctx.scratch_root}/{job_id}/maps", "name": spec.name, "ext": spec.ext,
                    "tokens": tokens, "items": [list(i) for i in items], "maps": maps, "has_udim": has_udim,
                    "numbering": numbering,
                    # Frozen per job, like the input conversion: the repo's OCIO config.
                    "ocio": OC.ocio_setup(ctx.cfg.ocio, pc)}
        shown = next((m for m in maps if m["color"]), maps[0])
        what = T.describe(len(maps), items, has_udim, numbering)
        # More tiles than fit in one run (textures.batch), or tiles of different sizes (one
        # run's images are one batch, all one size): a set of batches, filling one folder.
        batches = None
        if len(items) > 1 and (spec.batch or pc.validate_inputs):
            sizes = I.work_sizes(seq, names, wf.work_resolution) \
                if pc.validate_inputs and job_id != "preview" else [None] * len(items)
            ranges = T.split_batches(sizes, spec.batch)
            if len(ranges) > 1:
                batches = ranges
                what += f" in {len(ranges)} batches of up to {max(b - a for a, b in ranges)}"
        plan.update(out_dir=out_dir, version=v, picked=bool(picked), output_key=out_dir + "/", textures=textures,
                    first_frame=seq.first,
                    first_file=T.file_name(spec.name, shown, tokens, *items[0]),
                    last_file=T.file_name(spec.name, shown, tokens, *items[-1]) if len(items) > 1 else None,
                    what=what, asset=asset, batches=batches, units=len(items))
        return plan

    def info(self, plan: dict, ctx: PlanContext) -> dict:
        return {"textures": plan["textures"]}          # every map's file names, frozen per job

    def request(self, plan: dict) -> dict:
        return {"textures": plan["textures"]}          # where the runner writes the maps' images

    def summary(self, plan: dict) -> dict:
        return {"maps": [m["channel"] for m in plan["textures"]["maps"]]}

    def collects(self, job: dict) -> bool:
        return bool((job.get("info") or {}).get("textures"))

    async def prepare(self, ctx: RunContext) -> None:
        tex = ctx.info["textures"]
        # Empty, so a second attempt can't mix its map images with the first one's.
        try:
            await asyncio.to_thread(shutil.rmtree, tex["raw_dir"], True)
            await asyncio.to_thread(os.makedirs, tex["raw_dir"], exist_ok=True)
        except OSError as e:
            raise PrepareError(f"Can't create the scratch folder {tex['raw_dir']}: {e.strerror or e}") from None

    async def collect(self, ctx: RunContext) -> tuple[dict, list[str]]:
        info = ctx.info
        out_dir = info.get("output_dir")
        tex = info.get("textures")
        if not (tex and out_dir):
            return {}, []
        last = [0.0]

        def progress(done: int, total: int) -> None:
            if now() - last[0] < 0.5 and done < total:
                return
            last[0] = now()
            ctx.spawn(ctx.report(f"writing EXR maps {done}/{total}"))

        return await O.write_maps(tex, out_dir, ctx.cfg.textures, ctx.cfg.convert, ctx.cfg.paths.mappings,
                                  progress), []


HANDLER = Textures()
