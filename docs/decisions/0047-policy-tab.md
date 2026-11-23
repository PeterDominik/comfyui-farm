# 0047. A read-only Policy tab on the dashboard

Date: 2026-11-22. Status: accepted.

## Context

The policy lives in a file in the bank folder (0043). An admin needs to see:

- what the manager has taken, and when;
- whether the file as it is now was refused;
- how full each quota is;
- which licences each definition carries.

`GET /api/v1/policy` has the first two as JSON, but nobody reads JSON at a glance.

## Decision

- **A Policy tab on the dashboard**, in its own script (`static/policy.js`), like the
  Machines tab's environments. It is shown only when policy is on.
- **Read-only.** The policy is edited in the bank folder and checked with
  `comfyfarm policy check` (0043). The tab sends nothing, so it doesn't need the API keys
  that would make editing from the browser safe.
- **What it shows:**
  - where the rules in force came from and when, including whether they came from the
    manager's saved copy;
  - the file's error, and a red banner when no valid file has ever been read;
  - the defaults (`other_shows`, `unknown_show`, `unlisted_models`, `contact`);
  - the show rules, licence rules and quotas, each as it reads ("every show but rnd*",
    "At most 3 jobs and 1000 frames queued or running, for each show");
  - what is queued or running against each quota, as that quota counts it, a full one
    marked;
  - the models and licences each definition lists, or that it lists none.
- **`GET /api/v1/policy` gains `quotas_now` and `definitions`,** so the tab reads one
  endpoint. It is read when the page loads, when the tab opens, and every 15 seconds while
  it is open.
- **The functions that decide what each cell says touch no page.** node runs them in a test,
  as for the environments panel.

## Consequences

- Anyone who can open the dashboard can read the rules and their reasons. That is the
  same as today: a refusal already shows its rule and reason to the artist.
- Editing the policy from the browser waits for API keys and the Rodeo login.
