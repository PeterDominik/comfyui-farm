# 0030. What each machine should have: kept by the manager, done by the agent's environment manager

Date: 2026-11-21. Status: accepted.

## Context

Releases are in the bank (0028), and agents report what they have built (0029). Phase 4's
next part is the desired state and the environment manager. The manager keeps what each
agent should have and sends it in the check-in reply. The agent fetches and verifies the
release, builds it beside the current release, checks it, switches between jobs, and
reports. The four actions are install, update, roll back and remove. Until API keys
exist, these changes start only from the manager's host (architecture plan, 15 November).

The check-in reply is a long poll: it returns as soon as it has something for the agent.
Anything it carries every time would end the long poll at once.

## Decision

- **The manager keeps, per machine and environment, a release's fingerprint, or none**
  (remove it). It is kept in its database (`env_wanted`), so it outlives a restart. An
  environment it doesn't name on a machine isn't managed there: what is built stays as it is.
- **`POST /api/v1/environments/<name>/want`** sets it: `{release: fingerprint | latest |
  none, machines: [...] (default all it knows), forget: true}`. The manager resolves
  `latest` and checks the release against its hashes before telling anyone. It takes the
  request only from its own host (a loopback address), because without API keys the
  user name a request gives isn't checked. `env want` on the manager's host calls it on
  127.0.0.1.
- **The check-in reply carries `environments` only when it changed:** after `env want`,
  after the agent checks in (hello), or when the agent's status says it holds another
  version than the manager's (compared by a hash), as after a lost reply. The long poll
  stays a long poll.
- **The agent's environment manager** (`agent/envman.py`) looks at each wanted environment
  when told, and every 60 s. It does one thing at a time, off the event loop:
  - **install or update:** fetch the release from the bank (the folder of its
    `manager.json`, else the manager's `share.dir` as this machine sees it), copy and
    check it, and build beside the current build. Only once the check passes does the new
    build become current (0024, 0028). The build before stays as previous.
  - **rollback:** the previous release wanted again. Its build is still there, so it is a
    switch, not a build.
  - **remove:** the environment's builds.
  - **wait while a job runs in it:** the process runner says which environments its runs
    use (`environments_in_use`), and nothing is switched or removed under a run.
  - **a release that failed** (a hash, the build, its check) isn't tried again until the
    manager asks for something else, or the agent restarts.
- **What it is doing goes with the status:** `env_work` per environment (installing,
  waiting, removing, failed with why, off), and `env_wanted`, the version it holds.
  `GET /api/v1/environments` shows `wanted` and `work` per machine beside what each has built.
- **Off by default:** `envs.managed` in `agent.yaml`. Off, the agent installs nothing and
  says so in `env_work`.

## Consequences

- A machine is set up once. After that its environments change from the manager's host:
  `env publish`, then `env want`.
- This is the mechanism without the policy. `env want` reaches every machine named at once.
  Rollouts come next: one canary machine (the A6000), then the others one at a time, each
  gated by the check, with pause and rollback.
- A build uses the network (pypi.org, the PyTorch index) and the disk, not the GPU: jobs go
  on while it builds.
- The farm's ComfyUI environment isn't managed this way yet (phase 4's last step).
