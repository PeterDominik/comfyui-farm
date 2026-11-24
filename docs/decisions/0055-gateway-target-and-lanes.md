# 0055. API definitions go to a gateway agent's lanes; outside providers are off until switched on

Date: 2026-11-23. Status: accepted.

## Context

Phase 11's step 2 (docs/plan.md) is the gateway: jobs that run at an outside provider
(fal.ai first) rather than on the farm's GPUs.

- Peter's plan ("AI Farm: Providers, Catalogue and Spending Plan") runs them on a second
  agent on the manager's machine. That agent has lanes instead of GPUs, and a new target,
  `gateway`.
- Draining the machine's GPU agent or restarting its ComfyUI must never touch API jobs.
- API calls inside the manager would make the manager a backend, against the layering
  rules.

The desk places a job by what each agent reports it can run. Only with strict preflight
does an agent that doesn't list a definition count as unable to run it. Without a rule of
its own, the desk could hand an API job to a GPU machine.

## Decision

- **A new runner type, `api`, and a new target, `gateway`.**
  - An API definition is a `process.yaml` whose runner block has four keys:
    - `endpoint`: the provider's model path, e.g. `fal-ai/nano-banana-2/edit`.
    - `upload`: `{input param: field}`. The input's files are uploaded to the provider and
      sent as URLs in that field. Several inputs to one field make a list, in the params'
      order.
    - `send`: `{param: field}` for the other params. Never an input, never the output
      folder.
    - `fixed`: `{field: value}`, sent as they are. An artist never sets these.
  - Like a process, it gets its params in `job.json` and writes frames (`writes: frames`).
  - Its targets are `[gateway]`, the default for an API definition. No other definition
    targets the gateway.
  - It names its provider (`provider: fal`): no runner type `api` default exists.
  - Mistakes are refused when it loads, saying where. Examples: an uploaded non-input, a
    sent output folder, a field that is both sent and fixed, ComfyUI wiring.
- **An API provider's file** (`providers/<name>.yaml`) has `runner: api` and
  `target: gateway`, and three more keys:
  - `adapter`: the gateway's code that speaks to it.
  - `lanes`: its jobs at once on a gateway, unless the gateway's own file says otherwise.
  - `keys`: the names of the keys the gateway's file holds. Never the keys themselves.

  `providers/fal.yaml` is the first: fal.ai, off-site, adapter `fal`, 2 lanes, keys
  `requests` and `billing`. Only an API provider may have these keys. An API provider
  targets the gateway, and no other provider does.
- **Outside providers are off until the manager's config switches them on:**
  `providers.enabled` in config.yaml, empty by default. The farm's own (target `pool`)
  always are. While a provider is off:
  - its definitions still load and are checked;
  - the menus (`GET /api/v1/catalog`) leave them out;
  - `plan` refuses them with a 403 that names the setting;
  - a queued job of a provider switched off since fails, saying so.

  Which shows may use a provider is the policy's (step 3).
- **Lanes in the desk.** A gateway agent says its lanes when it checks in
  (`hello: {lanes: {fal: 2}}`).
  - **Slots:** a gateway's are one per free lane of each provider. A GPU machine's are as
    before: its free GPU, then one ahead.
  - **Routing:** an API job takes only a gateway's lane of its provider. A gateway takes
    nothing else. So neither kind ever waits behind the other.
  - **What agents report:** a GPU machine isn't asked about API definitions, and a gateway
    isn't asked about the others. Neither is said to lack them (the Machines tab's "skips",
    the agent's problems).
  - **Run times:** an API job's are kept under the GPU type `gateway`.
  - **The forecast** runs a gateway's lanes side by side.
  - **The plan's waiting note** says when no gateway with lanes for the provider has
    checked in, or when the gateway is drained or offline.

## Consequences

- Nothing runs an API job yet: the gateway agent and its runner are the next pull
  request, `tools/mock_fal.py` with them. With fal.ai off, nothing changes for anyone.
- The forecast's lanes are per provider; one provider per gateway is the pilot's case.
- `TARGETS` and `RUNNER_TYPES` grew, so an older agent that reads a newer definition says
  it can't load it. Update the agents with the manager, as for any definition change.
