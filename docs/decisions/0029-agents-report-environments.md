# 0029. Agents report the environments they have built, with their status

Date: 2026-11-21. Status: accepted.

## Context

Phase 4's rollouts need the manager to know what each machine has. It can't tell when an
install has finished, which release a machine runs, or that a machine still runs the
previous release, unless the agent says so. Releases are in the bank (0028). Each build on
a machine has a `farm-env.json` saying when it was built, how long it took and where it
came from, and the `current` and `previous` pointers say which builds are in use (0024).

## Decision

- **The agent reads its builds folder** (`envs.dir`) every 30 s, off the event loop, and
  when it changes, sends its status at once. Its status carries `environments`: for each
  environment built there, `current`, `previous` and each build by fingerprint, with `ok`,
  `built_at`, `build_seconds`, `copied` and `origin` (the release with its commit, or the
  checkout). Only what is on disk: nothing is checked or run to report it. Folders whose
  names start with a dot (uv's cache and Pythons, uv itself, release copies) aren't
  environments.
- **The manager keeps each agent's last report.** A status without `environments`, from an
  agent older than this, leaves it as it was. It is shown in `/api/v1/workers` as each
  machine's `environments`.
- **`GET /api/v1/environments`** lists the bank's releases, newest first, beside what each
  machine that is checking in has built. It is read-only; anyone may look.
- **Nothing acts on it yet.** The desired state and the environment manager come next, and
  rollouts after them.

## Consequences

- A machine's `env install` or `env remove` shows on the manager within 30 s.
- A failed build leaves no folder (0024), so it doesn't show here. The environment manager
  will report its own failures when it installs.
- The report is a few hundred bytes per environment and goes with every status, which is a
  few seconds apart. It is small next to the GPU stats that go with it.
