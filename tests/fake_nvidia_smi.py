"""A stand-in for nvidia-smi, for the GPU probe's tests.

$FAKE_NVIDIA_SMI is JSON: {"out": what to print, "err": what to print on stderr,
"exit": its exit code, "sleep": seconds to wait first}. Its arguments are appended as a
JSON line to $FAKE_NVIDIA_SMI_LOG."""

import json
import os
import sys
import time

if os.environ.get("FAKE_NVIDIA_SMI_LOG"):
    with open(os.environ["FAKE_NVIDIA_SMI_LOG"], "a", encoding="utf-8") as f:
        f.write(json.dumps(sys.argv[1:]) + "\n")
spec = json.loads(os.environ.get("FAKE_NVIDIA_SMI") or "{}")
time.sleep(spec.get("sleep", 0))
sys.stdout.write(spec.get("out", ""))
sys.stderr.write(spec.get("err", ""))
sys.exit(spec.get("exit", 0))
