"""A movie (writes: movie): a video a run writes as its output, named after the job
(decision 0037). The generic route (base.Generic) does the rest; a definition says

    writes:
      movie: {ext: mp4}         # optional: mp4, mov, webm, mkv or avi

When the job writes no frames, its MP4 is also what the dashboard plays (the job's
`preview`), as a frame job's preview movie is. Beside frames, those keep their own."""

from __future__ import annotations

from .base import Generic, RunContext

VIDEO = ("mp4", "mov", "webm", "mkv", "avi")


class Movie(Generic):
    kind = "movie"

    def head(self, section) -> dict:
        spec = super().head(section)
        if spec.get("ext") and spec["ext"].lower() not in VIDEO:
            raise ValueError(f"writes.movie.ext: a video's extension ({', '.join(VIDEO)}), not {spec['ext']}")
        return spec

    async def collect(self, ctx: RunContext) -> tuple[dict, list[str]]:
        outputs, warnings = await super().collect(ctx)
        movie = outputs[self.kind]
        mp4 = next((i for i, f in enumerate(movie["files"]) if f.lower().endswith(".mp4")), None)
        if mp4 is not None and not (ctx.job.get("collect") or {}).get("frames"):
            outputs.update(preview=movie["files"][mp4], preview_posix=movie["files_posix"][mp4])
        return outputs, warnings


HANDLER = Movie()
