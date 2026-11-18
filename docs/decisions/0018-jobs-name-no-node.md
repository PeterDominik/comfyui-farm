# 0018. Jobs name no node: the manager plans by what a definition takes and writes

Date: 2026-11-18. Status: accepted.

## Context

Phase 2 ends when the manager imports nothing ComfyUI-specific. After 0016 and 0017 the
manager still planned jobs with three pieces of ComfyUI wiring from the definition:

- whether an input's loader node reads its whole folder, which decides how the input is
  staged;
- the frames' save node section, sent to the agent to say what to collect, and the
  preview video's node id, which the agent asked the runner for after the run;
- each texture map's save-node id, written into the stored request.

The agent already has the definition the job was planned with: the job's fingerprint
pins it (0013), and the agent refuses to build from any other copy.

## Decision

The manager plans with what a definition takes and writes, and nothing it stores or sends
names a node:

- an input says whether the runner reads its whole folder (`reads_folder`);
- the job's `collect` says the frames' file type and whether the run makes its own preview
  video (`{"frames": {"ext": "png"}, "preview": true, "movie": ...}`);
- texture maps in the request are named by channel.

The runner looks the nodes up in its own copy: the ComfyUI runner points each map's save
node at scratch by its channel, and `Runner.preview(run, result)` replaces
`Runner.files(result, node)` for the run's own preview video.

## Consequences

Jobs queued before this still run: their `collect` sections are read as "there is a
frames output" and "there is a preview video", and their maps also carry the channel.

Agents and the manager are updated together. An agent from before this change cannot
copy a preview video for a job planned after it, because it expects a node id; none of
the five workflows makes its own preview video, so this affects no job on the farm today.

For manifests, these facts are still worked out from the wiring (the loader class, the
`outputs` and `textures` sections). Saying them in a process.yaml's head, so the manager
needs no runner's wiring to read them, is the next step.
