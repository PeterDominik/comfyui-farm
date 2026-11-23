# 0053. Evals are farm jobs pinned to a machine, checked by its agent, gating rollouts and syncs

Date: 2026-11-23. Status: accepted.

## Context

Decision 0010 kept "one reference job per process to rerun after an environment change", and
0015 reserved `eval:` in a definition's head for it. Rollouts (0031) and ComfyUI syncs (0033)
check only that a machine still has what a definition needs: its environment built, its
nodes present. A node pack that installs cleanly but changes what CHORD writes, or a new
torch that breaks a process, passes. Phase 8's gate is that such a change fails its eval and
stops its rollout or sync.

## Decision

- **`eval:` in a definition's head** is the reference job. It is checked when the
  definition loads.
  - `inputs`: each input given a file, or a pattern and range, in the definition's own
    folder. That keeps the reference in the reviewed repo, beside what it tests.
  - `params`: its other params.
  - `checks`: one or more of these:
    - `files`, how many files of each extension named;
    - `size`, every image's width and height;
    - `like`, an output (a name or a pattern matching one) against a reference image in the
      folder, with a `max_error`.
  - It needs an output folder param, for the farm to point its outputs elsewhere.
- **An eval is an ordinary farm job,** so it tests the path an artist's job takes: planning,
  the agent's preparing and converting, the runner, collecting outputs. It differs in three
  ways:
  - **Pinned to one machine.** That machine takes it even while drained (`desk._claim`), and
    before any artist's job (priority 1000). Other jobs still never go to a drained machine.
  - **Its outputs go to the machine's scratch folder,** `evals/<definition>`, not a show's.
    - The farm plans the folder in its own scratch folder, which reaches each machine as
      `@scratch/...` and is read as that machine's own (`fileio.wire`). The inputs reach it
      as `@repo/...` the same way.
    - The agent empties the folder before each eval of the definition, so what is checked
      is this run's, and the latest stays for a person to look at. The farm writes nothing
      to show storage, deletes nothing there, and the sweeper leaves evals alone.
  - **The policy doesn't apply.** It is the farm testing a machine, for no show.
- **The agent checks.** It holds the files it wrote, and it has OpenImageIO
  (requirements.txt). The manager might not see a machine's scratch folder.
  - Before the run, its copy of each eval file must match the manager's: the SHA-256 of each
    comes with the job. A ComfyUI definition's fingerprint doesn't cover the files beside
    it.
  - After the outputs are collected, it runs the checks. One that fails fails the job, with
    which check and by how much in its error. A passed eval keeps what each check measured
    in its outputs.
  - `like` is the mean difference per channel, over the channels both images have. It is
    measured by OpenImageIO, or by the standard library for 8- and 16-bit PNGs
    (`fileio.media.png_pixels`), so the check works even where OpenImageIO's Python module
    is missing.
- **`comfyfarm eval <definition> [--machine ...]`** starts evals through
  `POST /api/v1/evals`, from the manager's host only like everything that changes the
  machines. It prints each check and what it measured, the way to set `max_error` from
  real runs.
- **The gate, `evals.gate`,** is off by default.
  - **A rollout** runs the evals of the definitions in its environment, fixed when it
    starts. Each machine is drained when its turn starts, so no artist's job uses the release
    before it passed. Its jobs finish first.
    - Once it has the release, its evals are queued.
    - They pass: it takes jobs again (unless it was drained before), and the rollout goes
      on.
    - The rollout's evals and what it drained are kept with it (`rollouts.gate`), so a
      restart loses nothing.
  - **A ComfyUI sync** runs the evals of the ComfyUI definitions the machine could run
    before. It runs them after its ComfyUI is back and checked, while it is still drained.
  - **What stops them:** a failed eval, or evals not over within `evals.minutes`. The
    rollout or sync stops at that machine, which stays drained for a person.
    - Rolling a rollout back gives the machine its jobs again, with the release it had.
    - Cancelling a failed one leaves the machine drained.
- **`gpu_test` and `chord_pbr` get evals.**
  - gpu_test inverts an image, which is exact, so `like` can be tight.
  - chord_pbr checks its five maps are written. Its `like` references come from a machine
    the studio trusts, by pull request.

## Consequences

- **Machine time:**
  - With the gate on, a rollout takes each machine out of service for its install and its
    evals, not only its jobs' end.
  - A sync runs each ComfyUI definition's eval on every machine: about a minute for CHORD.
- **GPU types:** a `like` reference made on one GPU type may differ slightly on another.
  `max_error` is set from what `comfyfarm eval` measures on every machine.
- **The eval file hashes** make a machine whose checkout lags refuse an eval. That is the same
  "update its checkout" the fingerprints already ask for.
- **Eval jobs on the dashboard:** they show in the Queue tab and count in job metrics and the
  Usage tab, as the farm's own GPU time, for no show.
