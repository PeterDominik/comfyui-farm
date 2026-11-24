# 0061. The providers' bills: read by the gateway when the manager asks, and settling the ledger

Date: 2026-11-24. Status: accepted.

## Context

Phase 11's step 4 is the ledger and caps. Its first two pull requests (0059, 0060) estimate
each paid job and hold the estimate against its show's cap until the job ends. Then the job
is charged, released or unsure. This record covers the third: the money each job actually
cost, from the provider's bill.

Peter's providers plan:

- the actual cost comes from fal.ai's billing events, read with a billing key (fal.ai's
  BILLING preset);
- the ledger is reconciled every night, per account;
- top-ups are recorded by an admin (`comfyfarm spend topup fal 50`), with a balance alert;
- a Spend tab, and a CSV per show for any dates;
- step 5's gate: the ledger matches fal.ai's billing for a week.

fal.ai's billing events (`GET https://api.fal.ai/v1/models/billing-events`):

- take `start` and `end` (ISO 8601, `end` exclusive, 90 days at most), `limit` and
  `cursor`;
- give one event per charge, with `request_id`, `endpoint_id`, `timestamp`, `quantity`,
  `unit`, `unit_price`, `cost_total` (USD, after discounts) and `cost_estimate_nano_usd`.

`GET /v1/account/billing?expand=credits` gives the account's `current_balance`. Both need
the billing key. Keys stay on the gateway's machine (0056), and the manager never holds one.

## Decision

- **The manager asks; the gateway reads.**
  - A read is wanted each night at `ledger.at` (with `ledger.nightly`, off by default), or
    when asked from the manager's host (`comfyfarm spend reconcile`,
    `POST /api/v1/spend/reconcile`, from the host only until API keys).
  - Each gateway checking in is then answered once with `bill: {since, until}`.
  - The gateway's API runner reads each provider's bill for that time with its billing key:
    every page, then the balance. It sends one `bill` message per provider, with `provider`,
    `account`, `events`, `balance` and `stand_in`, or `error` when the bill couldn't be read.
  - Neither message holds a key.
- **The time read** reaches back to the oldest row the bill hasn't settled (an hour before
  its job was queued), and at least a day before the last read that went through. A missed
  night is read the next. It reaches back 90 days at most, which is fal.ai's limit.
- **A bill is applied in one transaction** (`JobStore.apply_bill`):
  - its charges are kept in `bill_events`, never swept. A charge read again is the same row.
  - **Billed:** a row whose request the bill charges becomes `billed`, and its amount is what
    was charged (`cost_estimate_nano_usd`, else `cost_total`). Two charges for one request
    add up. A job still running is billed once it has ended.
  - **An unsure row** (its request's id never known) takes the first charge the farm can't
    place for the same endpoint, between its job's queueing and an hour after it ended.
  - **What the bill should show by now and doesn't**, for rows of the account read (or of no
    known account): the job ended `ledger.grace_hours` (48) before the end of the time read,
    and was queued inside it. A charged row is billed $0; an unsure row is released.
  - **A bill that couldn't be read changes nothing.** Its error is kept with the read.
  - Every read is kept in `bill_reads`, with its counts, its balance and whether it was the
    stand-in's.
- **Charges no job of the farm's is for** (other use of the same account) are kept and shown
  apart, and no alert is raised for them: the pilot runs on Peter's own account.
- **A billing key is for the bill alone.** Without one, or with a key file that can't be
  read, the gateway still sends jobs, and `agent --check` says the bill can't be read. A
  refusal says to check `keys.billing` and its BILLING permissions.
- **The stand-in has a bill and a balance.** It charges each job that finished well, when it
  finished. A failed or cancelled job isn't charged.
  - Its charges are $0 by default, since it takes no money. The stand-in's jobs on the pilot
    then settle at $0 and leave rnd's cap whole for step 5.
  - With `--bill`, it charges at its prices (by resolution), as fal.ai would. The tests use
    that.
  - It takes the billing key from `gateway.yaml` too.
- **Top-ups** are recorded from the manager's host (`comfyfarm spend topup <provider>
  <amount> [--account] [--note]`), each with who and when, into `topups`. The account is the
  one the ledger knows when there is only one.
- **Shown.**
  - `GET /api/v1/spend`: each show against its cap, each provider account (topped up, the
    farm's jobs, the balance, other charges, the last read), the rows for a show and dates,
    and the top-ups.
  - `format=csv` gives the rows for a spreadsheet.
  - The Spend tab draws them, shown when an outside provider is switched on.
  - `comfyfarm spend` prints the summary.
- **Alerts** (`alerts.bills`, on with alerts): a bill the gateway couldn't read when last
  asked, and a balance below `ledger.balance_below` (0, off by default).

## Consequences

- With `ledger.nightly` and a billing key, step 5's gate can be checked on the Spend tab: day
  by day, the farm's charges against fal.ai's.
- A charged job whose request fal.ai doesn't bill (a failure, a cancel in the queue) costs $0
  two days after it ended. Until then it holds its estimate.
- The grace is a guess at how late fal.ai's bill can be. A charge that comes after its row
  was billed $0 still bills the row at what was charged, if a later read reaches it. Each
  read reaches a day before the last one, so a charge more than a day later than that is
  missed. Until then the farm has under-counted that job.
- The pilot's `gateway.yaml` needs a billing key file before the bill can be read. For the
  stand-in, any made-up word is the key.
