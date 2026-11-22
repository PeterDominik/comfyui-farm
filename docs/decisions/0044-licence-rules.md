# 0044. Licence rules against the models each definition lists

Date: 2026-11-22. Status: accepted.

## Context

0007 puts licences in the farm's policy. 0015 gave each definition an optional
`models: [{file, licence}]` in its head, unused so far. Model weights carry licences, and
some (research-only, non-commercial) can't be used on client work. Which licence applies
to a model at Rodeo can be a contract matter, not only what the model card says.

## Decision

- **A definition lists the weights it loads** under `models:`, each with its licence.
  - `models: []` says it loads none.
  - Leaving `models:` out says nothing, and the policy treats that definition as
    `unlisted_models` says (allow by default, so the pilot's definitions keep running
    until their lists are written).
- **Licence rules in policy.yaml** (`licences:`), alongside the show rules, with the same
  parts: a name, shows (patterns), a reason, and the new `except:`. Each rule either:
  - `deny: [licences]`, refusing a job whose definition uses a model under one of them;
  - `allow: [licences]`, refusing any model under another licence.

  Licences are compared without case. The refusal names the file and its licence, the
  rule, its reason and the contact.
- **`except: [shows]` on every rule**, show rules included. A rule for every show but R&D's
  is `shows: ["*"]` with `except: [rnd*]`. `*` also matches a job whose show isn't known,
  so a rule for every show can't be dodged by leaving the show out.
- **Checked in `Farm.plan`** after the show rules, with the definition's own list, and on a
  set's Retry failed. Rule names are unique across show and licence rules.
- **Licence names are free text**, not checked against a list. A rule may name a licence
  before any definition uses it. `comfyfarm policy check` notes:
  - licences a rule names that no definition carries;
  - definitions that list nothing;
  - files `deploy/comfyui/environment.yaml` gives a definition that its `models:` leaves
    out.
- **The repo's definitions:** CHORD lists `chord_v1.safetensors` under
  `ubisoft-machine-learning-license-research-only-copyleft`, from its model card. The GPU
  test lists none. LTX, SAM 3 and Wan list nothing until Rodeo says which licences it goes
  by for them.

## Consequences

- Adding `models:` to a definition changes its fingerprint, like any head field: every
  machine updates to the same commit.
- A definition whose list misses a file passes rules it shouldn't. `policy check` catches
  misses for the ComfyUI definitions from the environment file; for processes, the list is
  the author's word.
- `unlisted_models: deny` is the strict setting once every definition lists its models.
