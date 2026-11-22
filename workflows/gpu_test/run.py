"""The farm's test process (workflows/gpu_test, decision 0027), run by the process runner in
the gpu-test environment: python run.py, with $COMFYFARM_JOB naming its job.json.

It reads the input frames onto the GPU with torch, blurs, inverts or grades them there, and
writes them as <frames_prefix>_00001.png, ... into the output folder, where the farm renames
them. With hold_gb it first keeps that much of the card, which only fits if nothing else
holds it. It reports progress in events.jsonl, stops when the cancel file appears, says what
went wrong in result.json, and writes its report where job.json's writes.file says (the
farm names it after the job, <source>.gputest_vNNN.json): the card, how much of it was
free when it started, and the most it used.

torch, numpy and PIL are imported when they are needed, so the farm's tests can read this
file and run its frame reading and writing without them."""

from __future__ import annotations

import json
import math
import os
import sys
import time

GB = 2**30


class Job:
    """job.json, and the files the farm watches."""

    def __init__(self, path: str):
        with open(path, encoding="utf-8") as f:
            self.d = json.load(f)
        self.params = self.d.get("params") or {}

    def event(self, **e) -> None:
        with open(self.d["events"], "a", encoding="utf-8") as f:
            f.write(json.dumps(e) + "\n")

    def result(self, **r) -> None:
        with open(self.d["result"], "w", encoding="utf-8") as f:
            json.dump(r, f)

    def cancelled(self) -> bool:
        return os.path.exists(self.d["cancel"])

    def frames(self) -> list[str]:
        return list((self.d.get("input") or {}).get("files") or [])

    def out(self, i: int) -> str:
        """The i-th frame (from 1) as the farm collects it: it renames them to the job's names."""
        return os.path.join(self.d["output_dir"], f"{self.d['frames_prefix']}_{i:05d}.png")

    def report_path(self) -> str:
        """Where its report goes: as job.json's writes.file says, for the farm to name it after
        the job; without one (a job.json written by hand), beside the frames."""
        w = (self.d.get("writes") or {}).get("file")
        if w:
            return os.path.join(w["dir"], f"{w['prefix']}.json")
        return os.path.join(self.d["output_dir"], f"gpu_test.{self.d['job'][:8]}.json")


def load(path: str):
    """A frame as an H x W x 3 array of bytes (alpha dropped, 16-bit read as 8)."""
    import numpy as np
    from PIL import Image
    with Image.open(path) as im:
        return np.asarray(im.convert("RGB"))


def save(arr, path: str) -> None:
    """Whole or not at all: written under another name, then renamed, so a run stopped
    mid-frame leaves no broken PNG for the farm to collect."""
    from PIL import Image
    part = path + ".part"
    Image.fromarray(arr).save(part, format="PNG")
    os.replace(part, path)


def apply(torch, t, operation: str, strength: float):
    """One frame on the GPU, as floats 0..1 (H x W x 3)."""
    if operation == "invert":
        return 1.0 - t
    if operation == "grade":
        return (t * strength).clamp(0.0, 1.0)
    sigma = float(strength)
    if sigma <= 0:
        return t
    radius = max(1, math.ceil(sigma * 3))
    x = torch.arange(-radius, radius + 1, device=t.device, dtype=t.dtype)
    k = torch.exp(-(x * x) / (2 * sigma * sigma))
    k = k / k.sum()
    img = t.permute(2, 0, 1).unsqueeze(0)            # 1 x 3 x H x W
    pad = torch.nn.functional.pad
    conv = torch.nn.functional.conv2d
    # Separable: along rows, then along columns, each channel on its own.
    img = conv(pad(img, (radius, radius, 0, 0), mode="replicate"), k.view(1, 1, 1, -1).repeat(3, 1, 1, 1), groups=3)
    img = conv(pad(img, (0, 0, radius, radius), mode="replicate"), k.view(1, 1, -1, 1).repeat(3, 1, 1, 1), groups=3)
    return img.squeeze(0).permute(1, 2, 0)


def fail(job: Job, message: str) -> int:
    print(message, file=sys.stderr, flush=True)
    job.result(error=message)
    return 1


def main() -> int:
    job = Job(os.environ["COMFYFARM_JOB"])
    p = job.params
    files = job.frames()
    if not files:
        return fail(job, "the job has no input frames")
    import torch
    if not torch.cuda.is_available():
        return fail(job, "torch sees no GPU on this machine: check its NVIDIA driver (nvidia-smi)")
    dev = torch.device("cuda")
    name = torch.cuda.get_device_name(0)
    free, total = torch.cuda.mem_get_info()
    print(f"torch {torch.__version__} on {name}: {free / GB:.1f} of {total / GB:.1f} GB free", flush=True)
    torch.cuda.reset_peak_memory_stats()
    held, hold = None, float(p.get("hold_gb") or 0)
    if hold > 0:
        job.event(progress=0.0, stage=f"holding {hold:g} GB of the GPU ({free / GB:.1f} GB free)")
        try:
            held = torch.empty(int(hold * GB), dtype=torch.uint8, device=dev)
            held.fill_(1)                               # written, so the memory is really in use
        except torch.cuda.OutOfMemoryError:
            return fail(job, f"couldn't hold {hold:g} GB of the GPU: {free / GB:.1f} of {total / GB:.1f} GB were "
                             f"free when it started. Something else holds the card: see the job's warnings and the "
                             f"Machines tab")
    operation, strength, pause = p.get("operation", "blur"), float(p.get("strength", 4)), float(p.get("pause") or 0)
    t0 = time.monotonic()
    for i, src in enumerate(files, 1):
        if job.cancelled():
            return 0                                    # the farm asked: it reports the job cancelled
        frame = torch.from_numpy(load(src).copy()).to(dev, dtype=torch.float32) / 255.0
        out = (apply(torch, frame, operation, strength).clamp(0.0, 1.0) * 255.0 + 0.5).to(torch.uint8)
        save(out.cpu().numpy(), job.out(i))
        job.event(progress=i / len(files), stage=f"frame {i}/{len(files)}")
        end = time.monotonic() + pause
        while time.monotonic() < end:
            if job.cancelled():
                return 0
            time.sleep(min(0.1, end - time.monotonic()))
    torch.cuda.synchronize()
    peak = torch.cuda.max_memory_allocated()
    report = {"gpu": name, "torch": torch.__version__, "total_gb": round(total / GB, 2),
              "free_gb_at_start": round(free / GB, 2), "held_gb": hold, "peak_gb": round(peak / GB, 2),
              "frames": len(files), "operation": operation, "strength": strength,
              "seconds": round(time.monotonic() - t0, 2)}
    with open(job.report_path(), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(json.dumps(report), flush=True)
    warnings = []
    if free < 0.8 * total:
        warnings.append(f"Only {free / GB:.1f} of {total / GB:.1f} GB of the GPU were free when the test started: "
                        f"something else held the card")
    job.result(warnings=warnings)
    del held
    return 0


if __name__ == "__main__":
    sys.exit(main())
