# 0056. The gateway is an agent with the API runner alone; keys stay on it, and a job is never sent twice

Date: 2026-11-23. Status: accepted.

## Context

Decision 0055 sends API definitions' jobs to a gateway's lanes. This record is about what
runs them there: phase 11's step 2, second pull request.

- Peter's plan ("AI Farm: Providers, Catalogue and Spending Plan") puts the gateway on the
  manager's machine. That machine reaches the internet, and it already runs a GPU agent.
- The plan has one adapter per provider behind the same runner contract.
- fal.ai's keys are an account's, with its money. The pilot runs on Peter's personal
  account. The keys go on the gateway machine only: never in git, the bank folder,
  job.json, events or logs.
- No real fal.ai request is made before step 4's ledger and caps.
- fal.ai bills each request it takes, and keeps large results only a while.
- A process definition runs in a runner environment of its own (decision 0025). An API
  runner could too.

## Decision

- **The gateway is an agent.** It is `python -m comfyfarm agent --config gateway.yaml`, with
  `gateway.enabled` (`gateway.example.yaml`).
  - It has the API runner alone: no ComfyUI and no process runner.
  - It says its lanes when it checks in, from `gateway.yaml` or else
    `providers/<name>.yaml`. It sends `comfyui: null`, and its capacity is its lanes.
  - Everything else is the agent's as for any job: leases, reports, converting inputs (an
    EXR to PNG before upload), collecting frames, provenance.
  - Its scratch folder defaults to `data/scratch-gateway`. An agent empties the scratch
    folders of jobs it doesn't hold when it starts, so the gateway and the GPU agent must
    never share one.
  - Rollouts and ComfyUI syncs leave it out, and refuse it when it is named.
- **The API runner runs in the agent's own Python, with httpx**, which every agent already
  has: not in a runner environment of its own. The runner and its adapters are a few
  hundred lines of HTTP calls with nothing to build, and an environment would only add a
  rollout to keep in step with the farm's code. If an adapter ever needs a provider's SDK,
  that adapter gets an environment then.
- **Layout.** The runner is `comfyfarm/runners/api/runner.py`, and each provider's adapter
  is `comfyfarm/runners/api/<adapter>.py`, `fal.py` first. An adapter speaks to its provider
  and holds its keys; the runner does the rest for every provider alike. The API runner
  imports nothing of ComfyUI (`tests/test_layout.py`).
- **Keys.**
  - Each key is in a file of its own, named in `gateway.yaml`:
    `providers.<name>.keys.<key>`, the key names from `providers/<name>.yaml`.
  - The files are read when the gateway starts. A file that other users can read is
    refused (POSIX), and the provider can't take jobs until it is fixed.
  - Keys are held by the adapter only. They go in the Authorization header to the
    provider's own hosts and nowhere else.
  - The status, result and cancel URLs are built from the endpoint's owner and app and the
    request's id. They are never taken from an answer, so an answer can't send the key
    elsewhere.
  - A provider's error text has the key scrubbed out.
  - `tests/test_api_runner.py` runs jobs and checks the key is in no log line (DEBUG
    included), no message to the manager, no job or event, and no file the farm wrote.
  - git ignores `gateway.yaml` and `*.key`.
  - fal.ai's `billing` key is named in `providers/fal.yaml` and read only from step 4, for
    the ledger. Until then the adapter needs `requests` alone.
- **Never sent twice.**
  - What is sent goes into the run folder (`request.json`, state `sending`) before the call.
    The request's id is written as soon as the provider answers.
  - The call is shielded. A run dropped while its job is on the way still learns the
    request's id, and cancels it there.
  - A job is sent again only when it certainly never left: no connection, or the provider
    said 429 (too busy).
  - A lost answer, or a gateway that stopped between `sending` and the id, fails the job,
    saying to look in the provider's request history. It is never sent again.
  - After a restart, the run folder and the provider's status say where each run is
    (`locate`).
- **Each run.**
  - Its inputs are uploaded (fal.ai: a CDN token asked for with the key, then the CDN
    upload), and the job is sent once.
  - Its status is asked every `poll_seconds`.
  - What it made is downloaded at once into the output folder as
    `<frames_prefix>_<n>.<ext>`, checked to be the format the definition writes, and renamed
    by the frames handler as for any job.
  - Its time limit (`limits.max_minutes`, or `gateway.default_minutes`) counts the
    provider's queue too. When it passes, the job is cancelled at the provider.
  - Uploads and results are kept `media_seconds` (an hour by default), asked for with each
    upload and job (fal.ai's `X-Fal-Object-Lifecycle-Preference`).
  - `sync_mode` files that come inline (`data:` URLs) are read as files.
- **fal.ai's fields.** fal.ai names a field that takes several URLs `..._urls`, and one that
  takes one `..._url`. The adapter sends a list or one URL by that name. A definition whose
  `..._url` field could get more is broken, as is one whose `output_format` differs from
  `writes.frames.ext`. Both are checked the same on every machine.
- **Nothing reaches fal.ai itself before step 5.**
  - `REAL_SERVICE` in `runners/api/fal.py` is off, so every address must be on the
    gateway's own machine: `providers.fal.url` points at a stand-in.
  - Any other address, fal.ai's included, is refused before a byte is sent, and the
    provider shows as unable to take jobs, saying why.
  - Step 5 switches it on in a reviewed change, which also changes the test that pins it.
- **The stand-in.** `tools/mock_fal.py` is fal.ai's stand-in, with the standard library
  only.
  - It serves the upload token, the CDN upload, the queue and the result files on one
    address, on 127.0.0.1.
  - It checks the key as fal.ai does, and runs Nano Banana 2 edit in a few seconds, making
    grey PNGs.
  - It is stricter than fal.ai: it refuses a field its model doesn't know.
  - `MOCK_FAIL`, `MOCK_POLICY` and `MOCK_SLOW` in a prompt make a job fail, be refused by
    the content check, or run a minute.
  - Tests use it in memory, as an httpx transport.
- **The first definition.** `workflows/nano_banana2_edit_fal`:
  - its params are the input (one image), an optional reference, prompt, resolution and
    aspect ratio;
  - one PNG, with no web or image search, is fixed;
  - its time limit is 10 minutes, with `work_resolution: 2048`;
  - it isn't published or in ShotGrid's menu during the pilot.

## Consequences

- With `providers.enabled` empty (the default) nothing changes for artists. A gateway can
  now be started on the manager's machine against the stand-in, and runs a fal.ai job end
  to end. Starting it there, the Machines tab's lanes and the gate come with step 2's third
  pull request.
- "gateway" meant the manager in early code and scripts (`deploy/windows/run_gateway.bat`
  runs the manager). From 0055 on it means the API agent; the old uses are left as they are.
- fal.ai's multi-part upload (files over 100 MB) isn't done: such an input is refused,
  saying so. The pilot's images are far smaller.
- An API definition writes frames only, for now. Movies and files from providers come with
  the models that make them (step 9).
