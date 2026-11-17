# 0012. The agent talks to a resident runner through one in-process interface

Date: 2026-11-16. Status: accepted.

## Context

Decision 0002 puts every backend behind a runner. ComfyUI is a server that stays up with its
models loaded, so its runner cannot be a command started per job. The agent held ComfyUI's
client, websocket, queue checks, health check and environment itself, in one 1,100-line class.

## Decision

A runner is an object that lives as long as the agent (`runners/base.py`). The agent calls it
to start, cancel, look up and forget runs. The runner calls the agent back when a run starts,
progresses, succeeds, fails, is interrupted or is lost. Both share the run record the agent
keeps: the runner sets its id and its live progress, the agent everything else. The ComfyUI
runner (`runners/comfyui.py`) also keeps this machine's ComfyUI: health, node definitions,
environment, restarts. `agent/agent.py` imports no backend; a layering test holds that.

The file contract of 0002 (`job.json`, `events.jsonl`, `result.json`) is how the process
runner of phase 3 will talk to the command it starts. That runner implements this same
interface towards the agent.

## Consequences

The move changed no behaviour: the agent tests pass with only attribute paths renamed
(`agent.client` became `agent.runner.client`). The agent still sends ComfyUI's node
definitions and environment to the manager by name; phase 2 replaces that with what a runner
says it can run. `agent/launch.py` starts ComfyUI and belongs with the ComfyUI runner; it
stays where it is until its other users (`comfyenv` sync, the deploy scripts' tests) move too.
