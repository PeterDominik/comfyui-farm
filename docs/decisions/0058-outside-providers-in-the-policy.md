# 0058. Outside providers in the policy: none for a show until a rule allows it

Date: 2026-11-24. Status: accepted.

## Context

Phase 11's step 3 (docs/plan.md) is the off-site policy. A workflow at an outside provider
(fal.ai, `offsite: true` in `providers/fal.yaml`) sends the show's images and prompts
outside Rodeo, to an account that isn't the client's.

Peter's providers plan:

- the artist chooses the provider, and `policy.yaml` says which shows may use which;
- off-site is signed off in a config file managed by Peter's team, `policy.yaml` in the
  bank folder;
- the pilot runs on Peter's personal fal.ai account, so only `rnd` may use it: no client
  material reaches that account;
- off-site jobs need a known show.

`providers.enabled` in the manager's config.yaml (decision 0055) switches a provider on for
the whole farm. It doesn't say which shows.

## Decision

- **A `providers:` section in `policy.yaml`.** Each rule has a name, shows (patterns,
  `except`), and a reason, like every other rule. It does one of two things:
  - `allow: [outside providers]`: its shows may send work to them;
  - `deny: [providers]`: its shows may not use them, the farm's own included.
- **An outside provider is refused on every show until a rule allows it.** Every rule naming
  the show is checked, and a deny wins over an allow. The refusal names the deny rule, or
  says that no rule allows the show (`Policy: providers`), with the contact.
- **Its jobs need a known show**, from the input path or given, whatever `unknown_show`
  says.
- **With policy off, no show may use one.** The policy is the sign-off, so switching it off
  doesn't open the door. The farm's own providers are unaffected.
- **The rules are checked against `providers/`.**
  - A rule naming a provider the farm doesn't have is an error.
  - So is an `allow` naming one of the farm's own: they need no allowing, and the rule would
    look like it limits them.
  - `comfyfarm policy check` says this, lists the provider rules, and lists, for each outside
    provider, the shows that may use it.
- **Where it is checked**, with the definition's provider:
  - when a job is planned;
  - in the menus;
  - again when a machine would take a queued job, so a rule taken away fails the jobs it
    allowed (decision 0046).
- **Menus and evals.**
  - A menu drawn for a show marks that show's refusals.
  - A menu drawn without a show (the dashboard's) lists an outside provider's workflows if
    a rule allows the provider on some show. The job's own show is checked when it is
    planned.
  - The farm's own reference jobs (evals, decision 0053) skip the show rules but not this
    check. They have no show, so one at an outside provider is refused until evals of paid
    models get their show (steps 4 and 5).
- **`policy.example.yaml` has the pilot's rule:** `pilot-fal-on-rnd` allows fal on `rnd`.
- **The dashboard's Policy tab** shows the rules under *Provider rules*.

## Consequences

- After this change, fal.ai jobs on the pilot are refused until the manager's policy is on
  and its `policy.yaml` has a rule allowing fal for `rnd`.
- Who may submit to an allowed provider is anyone on that show (Peter's answer). The user
  name is trusted until API keys come, as for every rule.
- How much a show may spend is the ledger's: step 4.
