# 0062. fal.ai live: a gateway.yaml setting, with fal.ai keeping as little as it can

Date: 2026-11-24. Status: accepted.

## Context

Phase 11's step 5 is the fal.ai pilot: Nano Banana 2 edit on `rnd`, with a $50 cap, on
Peter's personal fal.ai account. Its gate: the job runs from Nuke on `rnd`, and the ledger
matches fal.ai's billing for a week.

Until now nothing could reach fal.ai: `REAL_SERVICE` in `runners/api/fal.py` was off
(decision 0056), so every address had to be a stand-in on the gateway's own machine. Steps 2
to 4 were built and tested against that stand-in.

fal.ai's documentation (read 24 November 2026) gives three settings that matter here:

- `X-Fal-Store-IO: 0` on a request keeps its JSON inputs and outputs out of the account's
  request history. That history is kept 90 days by default. The setting doesn't change how
  long files stay on the CDN.
- `X-Fal-Object-Lifecycle-Preference` sets how long the request's files stay on fal.ai's
  CDN. The gateway already sends it (`media_seconds`, an hour).
- Fallbacks are on by default: fal.ai may send a request to another endpoint it thinks is
  equivalent. `x-app-fal-disable-fallback: true` turns that off.

Nano Banana 2 edit also takes `sync_mode`: "the media will be returned as a data URI and the
output data won't be available in the request history." fal.ai's Python client uploads by
asking for a token at `rest.fal.ai/storage/auth/token?storage_type=fal-cdn-v3`, then posting
to `https://v3.fal.media/files/upload`. It sends both `X-Fal-Object-Lifecycle-Preference` and
`X-Fal-Object-Lifecycle`.

## Decision

- **A setting replaces the constant.** `live: true` for a provider in `gateway.yaml` sends
  its jobs to the provider itself. It is off by default, so nothing reaches fal.ai until an
  admin turns it on on the gateway's machine.
  - Without `live`, as before: only a stand-in on this machine, named by `url`.
  - With `live` and a `url` both set, the gateway refuses to send anything and says to
    choose one.
  - The constant is gone, so turning fal.ai on or off needs no code change.
- **Where things may go with `live`:**
  - the requests and billing keys go to fal.ai's queue, REST and API hosts only, over https;
  - the CDN token goes to its CDN (`*.fal.media`) only;
  - results are downloaded from its CDN alone, over https. Until now any https address was
    taken.
  - Uploads go to the CDN's own address, as fal.ai's Python client does. They carry both
    lifecycle headers.
- **fal.ai keeps as little as it can:**
  - every job is sent with `X-Fal-Store-IO: 0`, unless `keep_payloads: true` for the
    provider;
  - Nano Banana 2 edit's definition fixes `sync_mode: true`, so its result comes back in the
    answer and is never a file on the CDN;
  - the uploaded inputs stay `media_seconds`, an hour.

  The pilot runs on `rnd` alone (decision 0058), so no client material reaches the personal
  account. These settings keep the same habits for Rodeo's account later.
- **The reviewed endpoint only.** Every job carries `x-app-fal-disable-fallback: true`. A
  definition's review priced one endpoint, and the ledger estimates from that endpoint's
  price.
- **The check tries the keys for free.** With `live`, `agent --check` (`farmctl.sh check
  gateway`) asks fal.ai two questions it answers at no charge: the definitions' prices, with
  the requests key, and the account's balance, with the billing key. It says whether each
  key worked, and never sends a job.
- **The tests never reach fal.ai.** The live paths are tested against the stand-in. It
  answers for fal.ai's hosts inside the test process, with its files on the CDN's address.

## Consequences

- Step 5 on the pilot is configuration, not code:
  - fal.ai's real keys in the gateway's key files;
  - `live: true` and an empty `url` for fal in `gateway.yaml`;
  - the check;
  - a gateway restart.

  Turning it off again is `live: false`.
- The definition's fingerprint changes with `sync_mode`, so the gateway and the manager are
  updated together, as on the pilot they are.
- fal.ai doesn't document whether `X-Fal-Store-IO: 0` changes what the queue gives back. The
  pilot's first job shows it. If the result can't be read, `keep_payloads: true` is the
  fallback, and a later record says so.
- A personal account's other use shows up on the bill as charges no job of the farm's is
  for (0061). The gate's week compares the farm's charges with fal.ai's.
