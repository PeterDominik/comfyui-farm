# 0028. Environment releases: the repo's environment, published to the bank, checked by hash

Date: 2026-11-21. Status: accepted.

## Context

Phase 4 has the manager install, update and remove runner environments on its agents, so
that a machine is set up once. The design (architecture plan, 15 November) has five parts:

- the environment spec in git (0024);
- a release, an immutable hashed copy the manager publishes to the bank;
- the desired state per agent, kept by the manager and sent in the check-in reply;
- an environment manager in the agent that fetches, verifies, builds, checks and switches;
- rollouts: a canary, then the rest one at a time, with pause and rollback.

Agents accept only releases the manager published, and check their hashes. Until API keys
exist, rollouts start only from the manager's host. This record is the release.

Today a machine builds an environment from its own checkout (`env install`), so what it
builds depends on when its checkout was last pulled. The bank (`share.dir`) is the folder
every agent reads already: it holds `manager.json`, which agents find the manager through,
and only Peter writes to it during the pilot.

## Decision

- **A release is the environment's four files as committed,** copied to
  `<bank>/environments/<name>/<fingerprint>/` with `release.json`: the name, the
  fingerprint, the Python, each file's sha256, when and by whom it was published, and the
  commit. The same fingerprint is the same release: it is written once, to a temporary
  folder renamed into place, and publishing it again leaves it as it is.
- **`env publish <name>` publishes, on the manager's host.** It takes the bank from
  `config.yaml`'s `share.dir`, or `--bank`. It refuses an environment that isn't locked,
  whose lock is out of date (0024), or that has changes not committed. A release is the
  reviewed repo's, and the commit it came from is kept in it.
- **A release is checked wherever it is used.** `release.json` must name the folder it is
  in. Every file must match its hash, no file it doesn't list may be there, and the files
  must make the fingerprint. A release that changed in the bank is refused.
- **An agent installs from a copy.** `env install <name> --release <fingerprint|latest>`
  copies the release to the machine (`<envs.dir>/.releases/`), checks the copy, and builds
  from it as from the checkout (0024). What is built is what was published, whatever
  happens to the bank meanwhile. The build's `farm-env.json` keeps its origin: the
  release, when it was published and the commit, or the checkout.
- **An agent finds the bank** as it finds the manager: the folder of the `manager.json`
  its `agent.yaml` names. Otherwise it uses `--bank`.
- `env releases` lists what the bank has, newest first.

## Consequences

- The next steps install from releases: agents report what they have built, the manager
  keeps what each one should have and says so in the check-in reply, and the agent's
  environment manager does the install.
- Releases build up in the bank; one is a few hundred kilobytes (the lock is most of it).
  Removing old ones comes with rollouts, which know which are still current somewhere.
- Whoever can write to the bank could change a release. The hashes catch a file changed
  after publishing, but not a release written from scratch by someone who can write
  there. That is the bank's trust (CLAUDE.md: shared folder), as for `manager.json`.
