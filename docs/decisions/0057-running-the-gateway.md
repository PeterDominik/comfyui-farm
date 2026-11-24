# 0057. Running the gateway: farmctl roles, a stand-in started from gateway.yaml, key files as editors save them

Date: 2026-11-24. Status: accepted.

## Context

Phase 11's step 2, third pull request: running the gateway (decision 0056) on the manager's
machine, and the step's gate, a stand-in fal.ai job from Nuke end to end.

Peter's first run of the gateway on the manager's machine showed three things.

- Every job failed with `UnicodeEncodeError: 'ascii' codec can't encode character '﻿'`.
  The key file began with a byte-order mark, which some editors put first. The gateway
  kept the mark as part of the key, and an HTTP header is plain ASCII. Nothing was sent.
- The example's key path, `/home/<user>/...`, was copied as it was, and `~` wasn't
  understood.
- `agent --check` said the same problem three times.

The manager and the GPU agents run under `deploy/linux/farmctl.sh` and `install_cron.sh`;
the gateway and the stand-in ran in terminals.

## Decision

- **Key files are read as editors save them.**
  - A byte-order mark first in the file is dropped (UTF-8 with BOM).
  - Surrounding whitespace and line ends go.
  - A key with any character a key can't have (not printable ASCII) is refused, saying an
    editor may have added it.
  - A file that isn't UTF-8 (UTF-16, say) is refused, saying to save it as plain text.
  - `~` in a key path is the account's home.
  - The stand-in reads its key the same way.
- **What's wrong with the gateway is said once.**
  - In the log, when the gateway starts: no longer when its runner is made, which
    `agent --check` does too.
  - In `agent --check`, a provider's problem shows under the provider. Its definitions
    say they wait for it.
- **farmctl roles.**
  - `deploy/linux/farmctl.sh start|stop|restart|status|ensure|log|check gateway` runs
    `comfyfarm agent --config gateway.yaml`, beside the machine's `agent`.
  - `install_cron.sh gateway` keeps it running.
  - `farmctl.sh start|stop|restart|status|log mock_fal` runs the stand-in.
  - The gateway runs on the manager's machine, which is Linux in the pilot, so there is no
    Windows script for it.
- **The stand-in reads gateway.yaml.** `tools/mock_fal.py --gateway gateway.yaml` answers
  where `providers.fal.url` sends fal.ai's jobs, and takes the key in its `keys.requests`
  file.
  - The gateway and its stand-in can't disagree on the port or the key.
  - A URL that isn't on this machine is refused.
  - `--port` and `--key-file` remain for running it by hand.
- **The Machines tab shows a gateway's lanes** (`static/gateway.js`):
  - `gateway · fal ×2` in place of its GPU;
  - no VRAM and no Free VRAM;
  - it isn't listed in the Runner environments panel.
- **The gate in CI** is `tests/test_e2e_gateway.py`. The manager, the gateway and the
  stand-in run as processes.
  - The Nuke node's own code submits Nano Banana 2 edit on the current frame, through the
    artist client. It uses the stand-in Nuke of `tests/test_nuke_node.py`.
  - The frame comes back on show storage, numbered like the input frame.
  - It also covers a refused job, the Machines tab's data, and `agent --check`.
  - It checks the key is in no process's log, the manager's database or any file the farm
    wrote.

## Consequences

- A key pasted into a file by any editor works, or is refused in plain words. The job no
  longer fails on a header.
- The README's runbook writes the key with `read -rs` in a `umask 077` subshell. It stays
  off the screen and out of the shell's history, and only its account can read the file.
- The gate's other half is Peter's: a stand-in job from Nuke on the manager's machine.
