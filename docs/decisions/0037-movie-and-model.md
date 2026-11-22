# 0037. Movie and model, each one file: phase 5's gate

Date: 2026-11-22. Status: accepted.

## Context

Phase 5's gate: a new output type needs one handler file and no core edit. 0036 built the
generic route and added `file` with it. The plan names two more kinds: movie and model.

## Decision

- **`artifacts/movie.py`**: a video a run writes as its output (`writes.movie`, its `ext` one
  of mp4, mov, webm, mkv or avi).
  - It is named after the job and put in the job's folder like any kind on the route.
  - When the job writes no frames, its MP4 is also the job's `preview`: what the dashboard
    plays, as it plays a frame job's preview movie. The dashboard serves previews as MP4.
  - Beside frames, the frames' preview stays.
- **`artifacts/model.py`**: weights a run trains, such as a LoRA (`writes.model`, its `ext`
  one of safetensors, ckpt, pt, pth, bin or gguf).
  - It goes into the job's output folder, named after the job, and its outputs give each
    file's size.
  - Putting a model where ComfyUI reads models (a library in the bank) is a step of its
    own, like publishing. It comes with the training runner (phase 11), with its own
    decision: it changes what every machine's ComfyUI loads.
- **The gate holds.** These two files are the only source files this pull request adds
  or changes. A test also writes a kind none of the farm's code knows (`sound`) as one
  module beside the handlers. A process job writing it then goes through the manager and
  an agent and comes back named after the job (`TheGate`, `tests/test_process_runner.py`).

## Consequences

- Phase 5 is done. Each kind of output is one file in `comfyfarm/artifacts/`. Frames and
  texture sets keep their own route for the fields clients and publishing read (0035,
  0036).
- Retention (phase 6) can find every file a job wrote from its `artifacts`.
