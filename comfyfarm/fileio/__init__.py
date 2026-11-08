"""fileio: files in, files out.

Everything the farm does with files: studio paths and how a machine sees them, what an
artist's input is (sequences, UDIM tiles, movies), the conversions that turn it into the
PNGs a workflow reads, and the outputs written back to show storage.

No web, no database, no ComfyUI, no job states: functions take plain data (a job's frozen
info, a path mapping, a scratch folder) and return results, or raise errors written for
the artist (fileio.errors). Standard library only, so the farm's own ComfyUI nodes can
import it too; the config objects passed in are only read by attribute.

- read only (the manager at submit, and the agent): paths, media, tools, ocio, inputs,
  naming, textures
- writing (the agent only): convert, outputs, preview, scratch
"""
