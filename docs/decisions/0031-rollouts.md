# 0031. Rollouts: one machine at a time, the canary first, gated by the check

Date: 2026-11-21. Status: accepted.

## Context

`env want` (0030) tells the machines it names what to have, all at once. A release that
breaks on a machine would then break on all of them together. The design names one canary
machine first, then the rest one at a time, each gated by the check and later by evals,
with pause and rollback. In the pilot the A6000 machine is the canary and the manager's host
goes last (architecture plan, 15 November). Phase 4's gate: an environment is installed,
updated and removed on all three machines from the manager's host, and a broken release
stops at the canary.

## Decision

- **A rollout is a release of one environment going to a list of machines in order:**
  `rollouts.order` in `config.yaml` first, in that order, then the others by name. It is
  kept in the manager's database (`rollouts`), so it outlives a restart.
- **One machine at a time.** The rollout tells the machine it is at to have the release
  (`want_environment`, 0030). It moves to the next machine only once that one reports the
  release current with its build checked (0029). The machines after it aren't told anything
  until then.
- **It stops where a machine fails:** its environment manager says the install failed (a
  hash, the build, the check) or is off, or it doesn't have the release within
  `rollouts.step_minutes` (30). The rollout is then `failed` there, with the machine's
  reason. A broken release stops at the canary.
- **Pause, resume, rollback, cancel.** Pause stops it before the next machine, and resume goes
  on. Rollback tells every machine it reached to go back to what that machine had when the
  rollout started. That release's build is still there, so it is a switch; a machine that
  had none removes it. Cancel stops it and leaves the machines as they are. A failed
  rollout isn't resumed: it is rolled back or cancelled. A machine skips a release that
  failed on it until it is asked for another (0030).
- **One open rollout of an environment at a time** (running, paused or failed). Another
  environment's rollout can run beside it.
- **From the manager's host only,** like `env want`: `POST /api/v1/environments/<name>/rollout`
  (start) and `.../rollout/<pause|resume|rollback|cancel>`, called by `env rollout` on
  127.0.0.1. `GET /api/v1/rollouts` and `env rollouts` list them. The manager checks every
  rollout once every 5 s.
- **`environments/canary-test`** is broken on purpose: Python only, and a check that always
  fails. Publishing and rolling it out shows the gate. `gpu-test`'s description changed in
  the same pull request, which gives it a new release to roll out all the way.

## Consequences

- Once a release is published, `env rollout` replaces `env want` for changing environments.
  `env want` stays for one machine by hand.
- A rollout is gated only by each install's check. Evals (phase 8) can gate it later.
- The process runner still checks a machine's build against its own checkout's
  environment (0025). With rollouts, the release a machine should have and its checkout
  can differ for a while: a process job is refused on a machine that hasn't the checkout's
  build, until the rollout reaches it. One source for both comes with agent self-update
  (phase 9).
- A machine that is off stops the rollout once `step_minutes` pass. Start it, then roll
  back or cancel and start the rollout again.
