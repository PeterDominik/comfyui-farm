# Runbook

What to do when something on the farm goes wrong, written for the pipeline team, with no
Claude session open. Each section starts from what you see and ends with the farm working
again. The README says how each part works; this says what to do.

## The pilot

| Machine | Role | Checkout | Kept running by |
|---|---|---|---|
| `wrk1125l-3090` (Linux, RTX 3090) | the manager, and an agent | `/mntx/raid/pdominik/comfyui-farm` | cron (`install_cron.sh`), if set up |
| `tor-flame02-a6000` (Linux, RTX A6000) | an agent; the canary | `/softwareLocal/comfyfarm/comfyui-farm` | cron (`install_agent.sh` adds it) |
| `wrk59467w-5090` (Windows, RTX 5090) | an agent; also used for development | `D:\comfyui-farm` | `run_agent.ps1`, from `install_startup.ps1` |

- **The dashboard:** `http://wrk1125l.rodeofx.com:9090`. Its Machines, Queue and (when
  alerts are on) Alerts tabs are where to start.
- **The shared folder:** `/rdo/bank/comfy_farm` (`B:\comfy_farm` on Windows). It holds
  `manager.json` (where everyone finds the manager), the artists' client, the environment
  releases and `policy.yaml`.
- **The manager's state:** `data/comfyfarm.db` in its checkout. It holds the queue, the
  job metrics, the drained machines, the rollouts and the alerts. Back it up before
  touching it; never delete it to fix something.
- **Logs:**
  - **On every machine, in its checkout:**
    - the agent: `logs/agent-<name>.log`;
    - its ComfyUI, when the agent started it: `logs/comfyui-<name>.log`;
    - on Linux, the console output: `logs/agent.out`.
  - **On the manager's host:** `logs/comfyfarm.log` and `logs/manager.out`.
- **Running the farm's commands:**
  - On Linux, in the checkout:
    `env -u PYTHONPATH -u PYTHONHOME "$(cat run/python)" -m comfyfarm ...`. The pipeline's
    Python settings must not reach the farm's Python.
  - On Windows, in the checkout: `.venv\Scripts\python.exe -m comfyfarm ...`.
  - Below, `comfyfarm ...` means whichever applies.
- **Who to ask:** Peter Dominik. Changes to the farm's code go through pull requests on
  GitHub; nobody pushes to `main`.

## Where to look first

1. **Machines tab.** Each machine's state, in a word:
   - `ready` or `busy`: fine.
   - `offline`: its agent isn't checking in, or its ComfyUI isn't reachable. The row's
     error says which ("The agent hasn't checked in for 312 s.").
   - `ComfyUI connecting`, `checking nodes`: it is starting; give it a minute.
   - `drained: no new jobs`: someone (or a rollout or sync, named in the tooltip) took it out.
   - `can't run <workflow>`: it lacks something that workflow needs. The reason is under the
     row (see *A machine can't run a workflow*).
2. **Alerts tab**, when alerts are on: what needs a person, each with what to do.
3. **Queue tab.** A queued job's stage says why it waits. A failed job's error says why it
   failed, and *Timeline* shows each step with its time and machine.

## An agent has failed

You see: the machine `offline` on the Machines tab ("The agent hasn't checked in…"), or the
*Machine silent* alert. Its jobs go back to the queue after 5 minutes
(`farm.lost_after_seconds`) and run on the other machines. LTX jobs need 32 GB, so with the
5090 and the A6000 both down they wait.

1. **Take it out while you work on it.** On the Machines tab, *Drain* the machine. Nothing
   new goes to it, and the *Machine silent* alert stops for it.
2. **Is the machine up?** Log in to it (ssh, or remote desktop for the 5090). If it is off
   or asleep, start it: the agent comes back by itself (cron within a minute on Linux; on
   Windows, at logon or within 5 minutes with `-Always`).
3. **Is the agent running?**
   - Linux, in its checkout:
     ```bash
     deploy/linux/farmctl.sh status agent
     deploy/linux/farmctl.sh start agent      # if it isn't
     ```
   - Windows, in its checkout (restarts it whichever way it is kept running):
     ```powershell
     powershell -ExecutionPolicy Bypass -File deploy\windows\install_startup.ps1 -Stop -StartNow
     ```
4. **Why did it stop?** Read the end of `logs/agent-<name>.log`, or, on Linux, follow it with
   `deploy/linux/farmctl.sh log agent`. If it starts and dies again, the reason is the last
   lines.
5. **Check the machine.**
   - Linux: `deploy/linux/farmctl.sh check agent`.
   - Windows: `.venv\Scripts\python.exe -m comfyfarm agent --check`.

   It checks the manager and the agent's token, ComfyUI and its nodes, show storage, the
   scratch folder and the tools, and says what is wrong with each. Fix what it lists. The
   usual ones:
   - **Can't reach the manager:** the manager is down (see *The manager has failed*), or
     `B:` isn't mapped on Windows (`run_agent.ps1` maps the drives in `run\drives.txt`).
   - **Token refused:** `token` in `agent.yaml` isn't the manager's `agents.token`.
   - **Show storage or the bank not visible:** a mount or a network drive. That is IT's,
     not the farm's.
   - **Scratch folder full:** the local disk. The agent empties the folders of jobs it no
     longer holds when it starts. Only with `convert.keep_failed` on does it keep failed
     jobs' folders, and those can be deleted. `evals/` holds the latest eval of each
     definition.
6. **Is its ComfyUI up?** The agent starts ComfyUI again when it dies, if `agent.yaml` has
   `comfyui_launch`. Its output is in `logs/comfyui-<name>.log`. On Linux,
   `deploy/linux/farmctl.sh status comfyui` says whether it runs. An out-of-memory error, or
   a node pack failing to import, is the usual reason: see *A machine can't run a workflow*.
7. **Give it its jobs again.** Once the Machines tab shows it `ready` (drained), *Resume* it.
   Run a small job on it (the GPU test, or a CHORD texture) before leaving it.

What happens to its jobs meanwhile:
- **Jobs it held:** they go back to the queue after 5 minutes, and another machine runs
  them.
- **A job whose ComfyUI crashed mid-run:** it is queued again once (`farm.max_attempts`),
  with a warning on the job. After that it fails.
- **After a restart:** a restarted agent finds its running jobs in ComfyUI's queue and
  history and finishes them.

## The manager has failed

You see: the dashboard doesn't load, and the Nuke panel can't submit. The agents keep
running what they hold and report it when the manager is back.

On `wrk1125l-3090`, in `/mntx/raid/pdominik/comfyui-farm`:

```bash
deploy/linux/farmctl.sh status manager
deploy/linux/farmctl.sh start manager       # if it isn't running
deploy/linux/farmctl.sh log manager         # if it starts and stops again: the reason is at the end
deploy/linux/farmctl.sh check manager       # workflows, tools, OCIO, storage, the shared folder
```

- **The machine itself is down:** start it. With cron set up, the manager starts within a
  minute.
- **It runs but nobody reaches it:** the port (9090) is blocked by the workstation's
  firewall. That is IT's.
- **It says the database is locked or damaged:**
  1. Stop it.
  2. Copy `data/` aside.
  3. Ask Peter before anything else.

## A job failed or waits

- **It waits.** Its stage on the Queue tab says why:
  - **"The machines that can run it (…) are drained" or "offline":** see the Machines tab.
  - **"No machine on the farm can take it now":** its workflow needs more than any machine
    has (LTX needs 32 GB).
  - **Nothing starts at all,** and the Policy tab says no valid policy has been read (the
    *Jobs waiting* alert says so too): see *Policy* below.
- **It failed.** Click it on the Queue tab:
  - The error says what went wrong, and the traceback is under it.
  - *Timeline* shows each step, how long it took and on which machine.
  - Its provenance file (`*.provenance.json`, beside the outputs) says what ran it.
  - *Run again* submits it again as a new version.
  - A texture set's *Retry failed* runs only its failed batches, into the same folder.
- **Errors that name a machine** ("The farm machine can't run this workflow", "can't open
  the input folder"): see *A machine can't run a workflow*, or check the path from that
  machine.
- **A job stuck in `preparing`:** its input is being converted (EXRs, movies). The agent's
  log says how far it got. Cancel it if it doesn't move, and look at the input.

## A machine can't run a workflow

You see: `can't run <workflow>` on the Machines tab, with the reason under the row, or jobs
failing with "The farm machine can't run this workflow".

- **"its copy of the workflow (…) and the manager's (…) differ":** the machine's checkout
  isn't at the manager's commit. Update it (see *Updating the farm's code*).
- **A missing node pack or model:** that machine's ComfyUI isn't the farm's.
  1. Drain the machine.
  2. Run `comfyfarm comfyui verify --config agent.yaml` on it. On Linux,
     `deploy/linux/farmctl.sh verify agent` does the same. It lists what differs from the
     farm's environment file.
  3. Run the manager's sync for that machine (see *Changing environments*), or
     `deploy/linux/farmctl.sh sync agent --apply` on it.
  4. Resume it once it shows `ready`.
- **A process definition (the GPU test): its environment isn't built.** On the Machines tab,
  below the machines, the environments table shows what that machine has. See *Changing
  environments*.

## Updating the farm's code

After a pull request is merged, every machine runs the new commit: the canary first, then
the 5090, then the manager's host. On each machine, in its checkout:

```bash
git pull && deploy/linux/setup.sh agent && deploy/linux/farmctl.sh restart agent          # Linux agent
git pull && deploy/linux/setup.sh manager && deploy/linux/farmctl.sh restart manager      # the manager
```

```powershell
git pull; deploy\windows\setup.ps1 -Agent; deploy\windows\install_startup.ps1 -Stop -StartNow
```

- Jobs carry on across these restarts.
- Check on the Machines tab that each machine comes back `ready`, at the same version.
- If the pull changed the farm's own ComfyUI nodes, an agent restarts its ComfyUI once it
  is idle. The tab says so meanwhile.

## Changing environments

All from the manager's host, in its checkout. The Machines tab shows each step, read-only.

- **A runner environment (the GPU test's `gpu-test`):**
  1. `comfyfarm env publish <name>`.
  2. `comfyfarm env rollout <name> latest` takes it to the machines one at a time, the A6000
     first.
  3. A machine that fails its install stops the rollout there. Read the error with
     `comfyfarm env rollouts`, then `comfyfarm env rollout <name> --rollback` (back to what
     the machines had) or `--cancel`.
- **ComfyUI:**
  - `comfyfarm comfyui sync --all` brings each machine's ComfyUI to the farm's environment
    file, one at a time. Each machine is drained while it is synced.
  - A sync that stops leaves that machine drained for you to look at: read
    `comfyfarm comfyui syncs`, fix it, then *Resume* it on the Machines tab.
- **With `evals.gate` on,** each machine also runs its evals before the rollout or sync goes
  on (see *Evals*). A failed eval stops it there, the machine drained.

## Evals

- **Running one:** `comfyfarm eval <definition>` (on the manager's host) runs a definition's
  reference job on every machine that may run it. `--machine <name>` runs it on one. It
  prints each check and what it measured.
- **When one fails:** the job's error says which check and by how much. Its outputs stay in
  that machine's scratch folder, in `evals/<definition>`: open them, and compare them with
  the definition's `eval/` folder in the repo.
- **What a failure means:**
  - **After a node pack or environment change:** the change is the likely cause. Roll back
    the rollout, or sync the machine back.
  - **With nothing changed:** the reference may be too strict for that GPU type. Ask Peter
    before changing `max_error`.

## Policy

`policy.yaml` in the shared folder says which shows may use what (README, *Policy*).

1. Before saving a change, run `comfyfarm policy check <file>` on a copy.
2. A file the manager can't read is refused: the last valid one stays in force, and the
   Policy tab says why.
3. If no valid file was ever read, nothing is planned and queued jobs wait. Fix the file:
   the manager reads it again once it changes.

## Alerts

When `alerts.enabled` is on, the manager checks every minute (README, *Alerts*). Each
alert clears by itself when what it is about is fixed.

| Alert | Go to |
|---|---|
| Machine silent | *An agent has failed* |
| Jobs waiting | *A job failed or waits* |
| Failures in a row | the jobs on the Queue tab; if the machine is the cause, *An agent has failed* or *A machine can't run a workflow* |
| Sweep problems | `data/sweeps/<date>.json` on the manager's host: what the retention sweeper couldn't account for |
| Rollout stopped, Sync stopped | *Changing environments* |

`comfyfarm alerts test` checks that the Google Chat webhook still works.

## Retention

The sweeper only reports for now (`retention.enabled`, deletion off). Each day it writes
`data/sweeps/<date>.json` on the manager's host: the expired jobs and the exact files it
would delete. It never touches a folder an artist picked. Anything listed as a problem is
something to look at before deletion is switched on.
