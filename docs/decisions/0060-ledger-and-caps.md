# 0060. The ledger and caps: each paid job held against its show's cap as it is queued, and never guessed down

Date: 2026-11-24. Status: accepted.

## Context

Phase 11's step 4 is the ledger and caps. Its gate: a job over its cap is refused before
submit, and racing submits never overspend. This record covers step 4's second pull request.
The first (0059) gives every paid job an estimate when it is planned. The third reads what
fal.ai billed.

Peter's providers plan:

- the ledger is a `spend` table, never swept, in US dollars, with each row's provider and
  account;
- the estimate is made at plan, reserved at submit in one transaction against the show's
  lifetime cap, and settled with the actual cost;
- one cap per show for the life of the show, not per month. Limits are per show only;
- pilot budgets: `other_shows` 0, `confirm_above` 5, `rnd` cap 50, `warn_at` 0.8;
- a confirm before a paid submit when the estimate is $5 or more. It is a courtesy: the cap
  is what is enforced;
- only shows the policy allows may submit paid jobs, with no user list.

A job can end without the farm knowing whether its provider had it. Decision 0056 already
covers two such cases: fal.ai's answer is lost as the job is sent, or the gateway stops
between writing what it sends and hearing back. The job is never sent again. The manager
can also lose track of a job being prepared, when the gateway stops checking in or the job
is cancelled. Until this pull request, such a job was queued again, and a second run would
be paid for again.

## Decision

- **policy.yaml's `budgets:`.**
  - `currency` (USD, the only one), `other_shows` (the cap of a show no rule names, 0),
    `confirm_above` (5), `warn_at` (0.8) and `rules`.
  - Each rule has `name`, `shows` and `except` (patterns, as in other rules), `cap` in US
    dollars, an optional `warn_at`, and `reason`. Each named show gets its own cap. The first
    rule naming a show gives it.
  - With no `budgets:` section, no show may spend money. The policy off or unreadable
    refuses paid jobs too, since 0058 already refuses outside providers then.
  - `comfyfarm policy check` and the Policy tab show the budgets.
- **The `spend` table: one row per paid job, never swept.**
  - It keeps: show (lower case), user, provider, account, definition and fingerprint,
    endpoint, quantity, unit, unit price, currency, estimate, `amount` (what counts against
    the cap now), state, the provider's request id, what the gateway said reached it,
    created and settled times.
  - It also has `actual` and `billed_at` columns, for the bill (the third pull request).
  - A show's spending is the sum of its rows' `amount`.
- **The cap is checked twice.**
  - At plan: the artist's warning. The preview says what the show has left after the job,
    and a job that doesn't fit is refused (403) with the rule named.
  - As the job is queued, in the job's own transaction (`JobStore.insert`, `BEGIN
    IMMEDIATE`), under the farm's submit lock. The show's spending is read, the cap checked,
    and the job and its row written, or neither. Twelve threads submitting at once for a cap
    that holds two leave two jobs and two rows.
  - A cap lowered later doesn't touch jobs already queued: they hold their money.
- **What the money becomes when the job ends.** This is worked out where every change to a
  job passes (`JobStore._changed`, beside the metrics row), so no ending is missed:
  - **charged**: the job is done, or its request's id is known. The estimate stands until
    the provider's bill is read.
  - **released**: it never left the queue, or its gateway said nothing was sent. Its amount
    becomes 0.
  - **unsure**: anything else. The provider may have it and the farm can't tell. It is held
    like a charge until the bill shows whether it did.

  The ledger never guesses down.
- **The gateway says what reached the provider.**
  - A paid job's `failed` and `cancelled` reports carry `submitted`, as `dispatched`
    already does.
  - The API runner's `submitted()` reads the run folder after a restart. It gives the
    request with its id once sent, without an id when the provider may have it (an answer
    lost; the exit reason is now `unknown`), and nothing when nothing left (not sent, or
    refused).
  - A report without `submitted` means nothing was sent. Once the request's id is known, a
    later report can't take it back.
  - What may have been sent becomes the job's API JSON.
- **A paid job is never queued again.**
  - Lost by its gateway, or by a gateway that stopped checking in, it fails. It says to
    look in the provider's request history and submit it again if it isn't there.
  - A gateway that restarts while preparing a job that never left prepares it again itself,
    as before.
- **A cancel while a paid job is prepared goes to its gateway.** The job stays `preparing`,
  `cancelling`, and its gateway stops it. The gateway also cancels it at the provider once
  it is sent, and reports what was sent. The farm's own jobs are still cancelled at once.
- **A yes at `confirm_above`.**
  - The plan marks such a job `confirm`.
  - A submit or a Run again without `confirm_cost`, or with less than the estimate, is
    refused (409). The answer carries `confirm`, the estimate, beside `detail`.
  - The dashboard, the Nuke panel, the Nuke node and the shared client ask, then send it
    again with the amount said yes to (`--yes` on the command line).
  - The server checks the yes, so a client can't skip it by accident. A price that rose
    after the yes asks again.
- **Shown.**
  - Each paid job's money is in its Queue row and its details, from `spend` on the job
    (GET /api/v1/jobs and the events): `static/spend.js`.
  - The Policy tab has a Budgets section: each rule's cap and what its shows have spent or
    hold (`spend_now`).
  - The alert rule `budgets` (on unless `alerts.budgets: false`) fires when a show reaches
    its `warn_at` share of its cap, and again at the cap itself.
- **One paid job is one job.** An API definition writes no texture sets, so a paid job is
  never a set of batches.
- **An eval of a paid definition is refused**, since 0058 needs a show for an outside
  provider. Charging evals to `rnd` waits until a paid definition has an eval.

## Consequences

- Paid jobs are refused on every show until `policy.yaml` has a `budgets:` section. The
  pilot's is in `policy.example.yaml` (rnd, $50).
- The cap rests on the show name the client sends, until the Rodeo login (phase 13).
- An unsure row stays held until the third pull request reads fal.ai's billing events:
  - matching rows by request id;
  - correcting charged rows to what was billed;
  - releasing unsure rows the bill doesn't show.

  Until then an admin can raise the cap. A row is never edited by hand in the database.
- A job lost while it was prepared now fails, where it used to be queued again. The gateway
  runs on the manager's machine, so this should be rare. It spends at most one job's money
  that may not have been spent.
