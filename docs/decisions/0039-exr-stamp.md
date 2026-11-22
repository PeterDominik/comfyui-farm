# 0039. Each EXR map carries its job and its provenance file in its header

Date: 2026-11-22. Status: accepted.

## Context

0038 puts a provenance file beside each job's outputs. Texture maps travel: they are copied
into an asset's folder, published, or opened in Nuke far from where the farm wrote
them. Once copied, a map no longer sits beside its provenance file. An EXR header can hold
any number of named attributes, and copying the file keeps them.

## Decision

- **The texture maps the farm writes** carry two string attributes, set by oiiotool as it
  writes each EXR (`--attrib:type=string`):
  - `comfyfarm:job`: the job's id;
  - `comfyfarm:provenance`: the provenance file's name in the map's folder
    (`provenance.json`, or `provenance.002.json` for a batch of a set).
- **Namespaced** with `comfyfarm:`, the way OpenImageIO names its own (`oiio:ColorSpace`),
  so they can't clash with another tool's. Nuke shows them as `exr/comfyfarm:job`.
- **The same setting as the provenance file** (`provenance.enabled`, frozen per job). The
  stamp points at that file, so a job without one gets no stamp. There is no second switch.
- **Only EXRs the farm writes itself.** Today those are the texture maps. Frames, movies
  and other files are written by the runner, and the farm would have to rewrite them to
  stamp them; their provenance file sits beside them.
- **`comfyfarm check`** writes its test colour map with a stamp, so a machine whose oiiotool
  won't take one fails the check before any job does.
- **A test with the real oiiotool** (in `requirements.txt`, so in CI on Linux and Windows)
  writes a colour map and a data map with the repo's OCIO config and reads the stamp back.

## Consequences

- A map found anywhere names its job, and the file to open beside the folder it came from.
- A map whose provenance file couldn't be written still names the file. The job's warning
  says so.
- Nothing else in the header changes: colour, bit depth and compression are as before.
- Each attribute costs a few bytes per EXR.
