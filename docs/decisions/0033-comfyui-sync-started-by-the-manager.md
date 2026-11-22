# 0033. ComfyUI sync started by the manager, one machine at a time

Date: 2026-11-21. Status: accepted.

## Context

Phase 4's last step: in the pilot, ComfyUI is updated by today's sync, started by the
manager (drain, stop ComfyUI, apply, restart, verify). After the pilot, a complete ComfyUI
release is built side by side, torch included (architecture plan, 15 November). Until now
`comfyfarm comfyui sync --apply` was run by hand on each machine, after draining it on the
dashboard. The plan's safety rules apply: changes start only from the manager's host until
API keys exist, only reviewed files are used, and one machine is out of service at a time.

## Decision

- **The manager runs a sync of the machines, one at a time,** in `rollouts.order` (the canary
  first). For each machine in turn it:
  - drains it, unless it was drained already;
  - waits until it holds no jobs, however long they take;
  - tells its agent to sync, in the check-in reply;
  - waits until the agent says it's done and the manager has checked its ComfyUI again;
  - then resumes it, unless it was drained before.
- **The agent's resident runner does the sync.** For ComfyUI, it does what
  `comfyfarm comfyui sync --apply` does by hand:
  - it plans from its own checkout's `deploy/comfyui/environment.yaml`;
  - it refuses while ComfyUI has a farm job, or prompts in its queue;
  - it puts the launcher's hold in place, so no jobs come and ComfyUI isn't started meanwhile;
  - it stops the ComfyUI the agent started (it refuses one it didn't start);
  - it does the steps in order, stopping at the first that fails;
  - it removes the hold and starts ComfyUI at once;
  - it waits until its node definitions are read, then compares again.

  It sends how it goes with its status (`sync`). Nothing in the message is run: the agent
  only uses its own checkout's files.
- **Pinned to the manager's host's file.** `comfyfarm comfyui sync --all` on the manager's
  host names the fingerprint of its own checkout's environment file and the farm's
  `extra_model_paths.yaml`. Line endings are counted as committed, so a Windows checkout
  matches. A machine whose checkout has another version refuses: its code is updated
  (`git pull`) first.
- **Checked before going on.** A workflow the machine could run before and can't after the
  sync stops the sync there. So does a failed step. So does a machine that hasn't finished
  within `rollouts.sync_minutes` (60) of being told; waiting for its jobs doesn't count
  toward that. A machine where the sync stopped stays drained, for a person to look at. The
  exception is one whose agent refused before touching anything (another file,
  `envs.managed` off): it is resumed. The machines after it are never touched.
- **The same switch as the other environments.** An agent syncs only with
  `envs.managed: true` in its `agent.yaml`. That is how a person on the machine says the
  manager may change its environments.
- **One sync at a time, cancel only.** A ComfyUI sync can't be rolled back: the old ComfyUI
  isn't kept, which the side-by-side releases after the pilot change. A sync is cancelled
  before the next machine. A machine already told finishes and stays drained.
- **Kept in the manager's database,** so a sync outlives a restart. A machine told again (a
  lost reply, or its agent restarted) says how its sync went rather than syncing again.
- **On the Machines tab** in the Rollouts panel, read-only, and in `comfyfarm comfyui syncs`.
- **The manager stays blind to the backend.** It names a runner (`comfyui`) and a file
  version, and judges by what agents report. It imports nothing of ComfyUI.

## Consequences

- Updating ComfyUI on the farm is one command on the manager's host, after the new
  environment file is committed and every machine's checkout updated.
- `comfyui sync --apply` by hand stays, for one machine, for a machine whose agent doesn't
  start ComfyUI, and for first set-up.
- What the sync never does stays by hand: torch, the driver, launch flags, Python, models,
  and the farm library's shared packs. A sync reports them, and goes on.
- `Launcher.stop()` repeats what `restart()` does to stop ComfyUI. `restart()` can use it
  in a pull request that only moves code.
