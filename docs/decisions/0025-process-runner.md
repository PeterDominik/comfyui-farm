# 0025. The process runner: a reviewed command in a runner environment, kept by a supervisor

Date: 2026-11-21. Status: accepted.

## Context

Phase 3 adds a second runner beside ComfyUI: any command, in one of the farm's runner
environments (0024), with a time limit. 0002 has the agent talk to it through files in a
job folder (`job.json` in; `events.jsonl` and `result.json` out; a `cancel` file), and
0012 has the runner meet the agent through the same in-process interface as ComfyUI.
The head's `limits.max_minutes` (0015) has been read and checked since phase 2, and is
acted on now. With no API keys, the manager trusts what a client says, so nothing in a job
may become a command (CLAUDE.md: identity).

## Decision

- **A process definition** is a process.yaml whose runner block names an environment and
  a command: `runner: {type: process, environment: gpu-test, command: [python,
  "{definition}/run.py"]}`. `python` first is the environment's Python. The farm fills in
  `{definition}` (the definition's folder), `{run}` (the run's folder) and `{job}` (its
  job.json); any other `{...}` is an error. The head is the same as any definition's.
  For now it writes frames only (`writes.frames`, `writes.preview: true | false`). Its
  fingerprint covers the process.yaml and every other file in its folder, line endings
  aside, so changing the script makes a new fingerprint. A machine whose copy differs
  refuses the job, as with ComfyUI workflows.
- **Each registry reads its own definitions.** The manager reads every runner's heads.
  ComfyUI's registry reads ComfyUI workflows, and the process runner's reads processes.
- **The agent holds its runners, ComfyUI first.** A job goes to the runner that knows its
  definition (`Runner.knows`). The first runner is the machine's resident backend: its
  status, node definitions, environment and Free VRAM are the ones the agent reports.
  The agent tells the manager what the runners can run together. Until the GPU claim,
  `max_inflight` counts the runs of every runner together.
- **A run** has a folder, `process_runner.runs_dir/<attempt id>`. In it, the agent writes
  `job.json` (contract 1): the job, attempt, process, fingerprint, `params`, `input` and
  `inputs` as the farm resolved them, `output_dir`, `frames_prefix`, `writes`, `seconds`,
  and the paths of `events`, `result` and `cancel`. The command writes `events.jsonl`
  lines (`{"progress": 0.4, "stage": "..."}`) and, on failure, `result.json`
  (`{"error": "<for the artist>"}`). It writes its frames into `output_dir` as
  `<frames_prefix>_<number>.<ext>`. The agent then collects them as it does a workflow's:
  renamed to the job's names, with a preview movie.
- **A supervisor per run** (`runners/process_run.py`). It is standard library only and is
  started with `python -I` in its own session, so it needs nothing of either environment.
  It locks `alive` for as long as it lives, writes `state.json` once the command has
  started, keeps what the command prints in `log.txt`, and writes `exit.json` when it
  ends. It keeps the time limit itself: `limits.max_minutes`, or
  `process_runner.default_minutes` (60). On a cancel or at the limit, it asks the command
  to stop (SIGTERM, or CTRL_BREAK on Windows), waits `grace_seconds` (15), then kills it
  with what it started: its process group on Linux, `taskkill /T` on Windows. On Linux the
  command also dies with the supervisor, and anything it left running is killed when it
  ends, so nothing holds the GPU unwatched.
- **Runs outlive the agent.** A new agent finds a run by its folder. With `exit.json`, the
  run is finished. With the lock held, it is running; the lock is tested, not a process
  id the system may have reused. With neither, the run is lost and the manager requeues
  it, as with a ComfyUI that died.
- **What a machine can run:** the runner reports each process definition. A problem is
  this machine's: the runner is off, or the environment isn't built at this checkout's
  version. Broken is the same everywhere: an environment the repo doesn't have, a command
  that isn't a list, an unknown placeholder. When an environment is built or removed, the
  agent tells the manager within 15 s. `agent --check` lists the same.
- **When it ends badly**, the artist reads `result.json`'s error, or else the exit code and
  the last line the command printed. For a time limit, the message names the limit. The
  log's tail goes with it as the traceback. A run that went well leaves nothing behind; a
  failed run's folder is kept for 7 days.
- **Off by default:** `process_runner.enabled` in `agent.yaml`.

## Consequences

- No process definition is in the repo yet; the test process (phase 3's next step) is the
  first one.
- Until the GPU claim, a process job and ComfyUI share the card. With `max_inflight: 1`
  they don't run at the same time, but ComfyUI keeps its models loaded, so a process that
  needs most of the card can run out of memory. The claim frees ComfyUI's VRAM (0023)
  before the process runner uses the card.
- The manager gives jobs only to an agent whose ComfyUI is up, as before. A machine that
  runs processes without ComfyUI comes later.
- On Windows, anything a command started and left running after it exited on its own is
  not killed; a job object would do that. Cancels and the time limit kill the whole tree
  while the command runs.
- A supervisor keeps running through an agent restart when the agent alone is stopped:
  `farmctl.sh` on Linux, and the scheduled task's stop on Windows. A service manager that
  stops the whole process tree stops it too; NSSM does by default. The run is then lost
  and requeued.
- `job.json` is contract 1. A field added later stays optional, so commands written for 1
  keep working.
