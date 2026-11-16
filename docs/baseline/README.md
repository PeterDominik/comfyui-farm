# Baseline timings

How long each workflow takes on each GPU, cold and warm, and where the time goes. The
refactors of stage 1 are checked against these numbers: a phase that makes jobs slower shows
up here.

`python tools/baseline.py` reads the jobs the farm has already run and writes
`baseline-<date>.md` and `.json` into this folder. It submits nothing and changes nothing.

To take a baseline on the pilot machines:

1. On the dashboard, drain every machine but one.
2. Submit a workflow from Nuke twice in a row, with the same input. The first run after a
   different workflow is the cold one; the second is warm.
3. Do that for each workflow you care about, then for the next machine.
4. Resume the machines, run `python tools/baseline.py --days 1`, and commit the two files.

A start counts as cold when the job before it on that machine ran another workflow, which is
the rule the manager places jobs by. A ComfyUI restart also makes the next run cold, and the
farm does not record that, so take the runs without restarting in between.
