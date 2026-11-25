# 0066. Workflows turned off in the manager's config: kept in the repo, in no menu or list

Date: 2026-11-25. Status: accepted.

## Context

On 25 November Peter asked, before step 9's next pull request: "disable all the ltx and sam3
workflows, keep them in the system, just disable them for now so they dont show up in
comfyfarm". They are `ltx25_alpha_gen`, `ltx25_layout_render` and `sam3_ltx_alpha`.

The farm had no way to do that. A definition in `workflows/` is offered everywhere: the
menus (decision 0054), the dashboard, Nuke, ShotGrid's menu, evals. `hidden: true` takes one
off artists' menus but leaves it submittable and on the dashboard. Taking the folder out of
the repo isn't keeping it, and the tests use these definitions as their fixtures.

Agents read the definitions from their own checkout, and run what the manager hands out.

## Decision

- **`disabled_workflows`, a list of definition names in the manager's `config.yaml`**, empty
  by default. A per-farm setting, like `providers.enabled` (0055): the repo stays what was
  reviewed, and the farm chooses what it offers.
- **A definition named there is still read and checked**, and kept aside
  (`DefinitionRegistry.disabled`), not with the others (`.workflows`). So everything that lists
  the farm's definitions leaves it out without a change of its own: the menus and the
  catalogue, `/api/v1/workflows`, the dashboard, the Nuke panel, ShotGrid's menu, evals, a
  machine's problems and skips.
- **Asking for it by name says it is off:** a plan or submit is refused, with its label, that
  it is turned off on this farm, and that it is in `disabled_workflows` in the manager's
  config.yaml.
- **A job of it queued before it was turned off fails when a machine would take it**, saying
  so, as a job whose provider was switched off does (0055): an agent's copy would still run
  it, and turning it off is the admins' choice. A job already on a machine finishes.
- **The policy still knows its name,** so a rule naming it stays valid for when it comes back.
- **The farm's ComfyUI environment still counts what it needs** (`comfyfarm comfyui snapshot`
  reads every definition in the repo): turning a workflow off doesn't take its node packs or
  models off the machines.
- **`comfyfarm check` lists it as `[ off]`.** A name that isn't a definition is a warning in the
  manager's log, not an error: the manager still starts.
- Agents and the gateway take no setting: they run only what the manager hands out.

## Consequences

- Turning a workflow off or on is a line in config.yaml and a restart of the manager.
- ShotGrid's menu drops it at its next sync; until then its right-click says the workflow
  isn't on the farm and to sync the menu.
- On the pilot, Peter turns off the three LTX and SAM3 workflows with
  `disabled_workflows: [ltx25_alpha_gen, ltx25_layout_render, sam3_ltx_alpha]`.
