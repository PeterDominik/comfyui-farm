"""A model (writes: model): weights a run trains, such as a LoRA, named after the job in its
output folder like any other output (decision 0037). The generic route (base.Generic)
does the rest; a definition says

    writes:
      model: {ext: safetensors}   # optional: safetensors, ckpt, pt, pth, bin or gguf

Its outputs give each file's size. Putting a model where ComfyUI reads models (a library
in the bank) is a step of its own, like publishing, for when the farm trains them."""

from __future__ import annotations

import os

from .base import Generic, RunContext

WEIGHTS = ("safetensors", "ckpt", "pt", "pth", "bin", "gguf")


class Model(Generic):
    kind = "model"

    def head(self, section) -> dict:
        spec = super().head(section)
        if spec.get("ext") and spec["ext"].lower() not in WEIGHTS:
            raise ValueError(f"writes.model.ext: a weights file's extension ({', '.join(WEIGHTS)}), not {spec['ext']}")
        return spec

    async def collect(self, ctx: RunContext) -> tuple[dict, list[str]]:
        outputs, warnings = await super().collect(ctx)
        outputs[self.kind]["bytes"] = [os.path.getsize(f) for f in outputs[self.kind]["files"]]
        return outputs, warnings


HANDLER = Model()
