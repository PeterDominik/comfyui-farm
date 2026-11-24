# 0059. Prices and estimates: a reviewed cost block per API definition, priced with the provider's price that day

Date: 2026-11-24. Status: accepted.

## Context

Phase 11's step 4 is the ledger and caps: every paid job charged to its show, and refused
before anything is sent when it would take the show past its cap. Everything in it starts
from an estimate made when the job is planned. This record covers the estimate, the first
of step 4's three pull requests.

Peter's providers plan:

- the manager prices a job from the provider's unit price and the job's units ("about $0.08
  for one image at 1K");
- fal.ai publishes each endpoint's billing unit and unit price through its pricing API
  (`GET https://api.fal.ai/v1/models/pricing?endpoint_id=...`, any key). The gateway reads
  them daily and the manager uses them;
- the definition's cost block says how many units a job is, from its parameters;
- the ledger is in US dollars, and a price in another currency is refused, never converted;
- a price that moved more than 10% since the definition was reviewed is flagged;
- an off-site definition lists its models, so licence rules apply to it.

fal.ai's pricing API gives one price per endpoint and unit. Nano Banana 2 edit's page says
$0.08 an image, 0.75× at 0.5K, 1.5× at 2K and 2× at 4K, and web search adds $0.015. The API
doesn't give those multiples.

## Decision

- **A `cost` block in the definition's head**, required for an API definition and refused
  on any other. It says:
  - `unit`: the provider's billing unit;
  - `price`: one unit's price in US dollars, as reviewed from the provider's page;
  - `currency`: USD only, and may be left out;
  - `quantity`: units a job is, a number or `{param: <a number param>}`, 1 if left out;
  - `by`: `{param: <a choice param>, factors: {<each choice>: <a multiple>}}`, one factor
    for every choice.

  Mistakes are refused when the definition loads, saying where.
- **The price isn't in the definition's fingerprint.** A price is the manager's business:
  reviewing it changes nothing on the gateway, which needn't be updated for it.
- **An off-site definition lists its models.** Nano Banana 2 edit lists
  `{file: fal-ai/nano-banana-2/edit, licence: google-via-fal}`, the plan's licence name, so
  licence rules can name it.
- **The gateway reads prices.**
  - When it starts and every `gateway.prices_hours` (24), it asks each provider's adapter
    for its definitions' endpoints: for fal.ai, the pricing API, with the requests key and
    with fal.ai's hosts guarded as for every other call.
  - It sends each definition's price with what it can run (`nodes`:
    `processes[name].price`, with when it was read), again when one changes.
  - A provider it can't reach keeps the prices it had.
  - The desk keeps each gateway's prices for definitions with the manager's fingerprint, and
    the farm takes the latest.
- **The estimate is made when a job is planned:** the provider's unit price that day × the
  `by` factor × the quantity. Until a gateway has read the provider's price, the reviewed
  price is used, and the estimate says so.
  - It shows in the dashboard's preview as `Cost`, and is kept in the job's `info.cost`. It
    is not a plan note, so it never becomes a job's warning.
  - A price that moved more than 10% from the reviewed one is used, and noted: that note is
    a warning.
  - A price in another currency than USD, or in another unit than the definition's, is
    refused (409): the definition needs reviewing before it runs again.
- **The stand-in** (`tools/mock_fal.py`) answers the pricing API, at fal.ai's prices for its
  models. Tests change them.

## Consequences

- Every paid job now has an estimate. The ledger and caps (step 4's second pull request)
  reserve it at submit and refuse a job over its show's cap. Billing events (the third)
  replace it with what fal.ai billed.
- Nano Banana 2 edit's fingerprint changes once, with its `models:` list, so the gateway and
  the manager are updated together, as for any definition change.
- Nuke shows the estimate with step 4's confirm dialog. Until then it shows in the
  dashboard's preview and the job's info.
