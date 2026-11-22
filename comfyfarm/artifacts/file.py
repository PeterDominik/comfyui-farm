"""Files (writes: file): anything a run writes besides frames and texture maps, such as a
report, named after the job (decision 0036). All of it is base.Generic's; a definition says

    writes:
      file: {ext: json}         # optional: only files with this extension

and gets <base>.json (plate.gputest_v001.json) from a process that writes
<prefix>.json into the folder job.json gives it (writes.file.dir and .prefix)."""

from __future__ import annotations

from .base import Generic


class File(Generic):
    kind = "file"


HANDLER = File()
