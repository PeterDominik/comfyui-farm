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
| [0012](0012-resident-runner-interface.md) | The agent talks to a resident runner through one in-process interface | accepted |
| [0013](0013-graph-built-where-the-job-runs.md) | The machine that runs a job builds its graph, from a stored request | accepted |
| [0014](0014-agents-report-what-they-can-run.md) | Agents report what they can run | accepted |
| [0015](0015-process-yaml.md) | A definition is a process.yaml: a shared head and a runner block | accepted |
| [0016](0016-manager-stores-requests-only.md) | The manager stores requests only, and keeps what a run submitted as the agent reports it | accepted |
| [0017](0017-environment-compared-on-the-agent.md) | Each agent compares its own environment and reports what differs | accepted |
| [0018](0018-jobs-name-no-node.md) | Jobs name no node: the manager plans by what a definition takes and writes | accepted |
| [0019](0019-the-head-says-what-a-definition-writes.md) | The head says what a definition takes and writes; the runner block, which node does it | accepted |
| [0020](0020-the-manager-checks-the-head.md) | The manager checks a definition's head; agents check how it wires its runner | accepted |
| [0021](0021-definitions-and-their-comfyui-half.md) | A definition's head is definitions.py; its ComfyUI half is runners/comfyui_workflow.py | accepted |
| [0022](0022-agents-probe-their-gpus.md) | Agents read their GPUs with nvidia-smi | accepted |
| [0023](0023-runners-free-their-vram.md) | A resident runner frees its VRAM on request, and only when idle | accepted |
| [0024](0024-environments-are-uv-projects.md) | Runner environments are uv projects in the repo, built on each machine with uv | accepted |
| [0025](0025-process-runner.md) | The process runner: a reviewed command in a runner environment, kept by a supervisor | accepted |
| [0026](0026-the-gpu-claim.md) | One runner's runs at a time on the card; the last one frees it first | accepted |
| [0027](0027-the-test-process.md) | The test process: a torch job that checks the process runner and the GPU claim | accepted |
| [0028](0028-environment-releases.md) | Environment releases: the repo's environment, published to the bank, checked by hash | accepted |
| [0029](0029-agents-report-environments.md) | Agents report the environments they have built, with their status | accepted |
| [0030](0030-wanted-environments.md) | What each machine should have: kept by the manager, done by the agent's environment manager | accepted |
| [0031](0031-rollouts.md) | Rollouts: one machine at a time, the canary first, gated by the check | accepted |
| [0032](0032-environments-on-the-machines-tab.md) | Environments and rollouts on the Machines tab: shown, not changed | accepted |
| [0033](0033-comfyui-sync-started-by-the-manager.md) | ComfyUI sync started by the manager, one machine at a time | accepted |
| [0034](0034-typed-artifacts.md) | Typed artifacts: one handler per kind of output | accepted |
| [0035](0035-the-core-leaves-outputs-to-their-handlers.md) | The core leaves each kind of output to its handler; texture sets stay the core's | accepted |
| [0036](0036-the-generic-route-for-a-new-kind.md) | The generic route for a new kind of output; files first | accepted |
| [0037](0037-movie-and-model.md) | Movie and model, each one file: phase 5's gate | accepted |
| [0038](0038-provenance-file.md) | A provenance file beside each job's outputs | accepted |
