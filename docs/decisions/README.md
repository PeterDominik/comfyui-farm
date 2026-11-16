# Decision records

One short file per design decision: what was decided, why, and what follows from it.
A record is not edited once merged, except for its status. A changed decision gets a new
record that names the one it replaces. `tests/test_docs.py` checks this index.

| No. | Decision | Status |
|---|---|---|
| [0001](0001-build-on-this-repo.md) | Build the AI layer on this repo; Tractor is a place to run jobs | accepted |
| [0002](0002-backend-blind-manager-and-runner-contract.md) | The manager is blind to the backend; runners sit behind a file contract | accepted |
| [0003](0003-definitions-from-reviewed-repo.md) | Process definitions come only from the reviewed repo and are pinned by hash | accepted |
| [0004](0004-job-classes-no-llm-serving.md) | Two job classes, and no LLM serving | accepted |
| [0005](0005-dispatch-targets.md) | Two dispatch targets, chosen per process | accepted |
| [0006](0006-retention.md) | Outputs are kept three days; a metrics row per job is kept | accepted |
| [0007](0007-policy-file-and-pilot-identity.md) | Policy is a file in the bank folder; identity is trusted for the pilot | accepted |
| [0008](0008-remote-runner-environments.md) | Runner environments are installed remotely from reviewed releases, never from scripts sent over the API | accepted |
| [0009](0009-workstation-windows.md) | Workstations join in timed windows | accepted |
| [0010](0010-observability.md) | Observability is a job timeline, GPU history, metrics and evals | accepted |
| [0011](0011-ci.md) | Tests run in CI on every pull request | accepted |
