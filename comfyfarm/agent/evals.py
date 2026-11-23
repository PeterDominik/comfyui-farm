"""An eval's part on the machine that runs it (decision 0053; the manager's part is
manager/evals.py).

Before it runs: this machine's copy of each eval file must be the manager's (their SHA-256
come with the job), and the eval's folder in this machine's scratch folder is emptied, so
what is checked is this run's. After the farm has collected its outputs: the checks, on
the files it wrote:

  files   how many files of each extension it wrote: {exr: 5}. Extensions not named aren't
          counted.
  size    every image it wrote is [width, height].
  like    an output (its file name, or a pattern matching exactly one) against a reference
          image in the definition's folder: their mean difference, per channel, 0 to 1 for
          8- and 16-bit images (EXRs: in their own values), at most max_error.

Images are compared with OpenImageIO when its Python module is installed (it is in
requirements.txt), else 8- and 16-bit PNGs with the standard library (fileio.media)."""

from __future__ import annotations

import fnmatch
import hashlib
import os
import shutil

from ..fileio.media import image_size, png_pixels

__all__ = ["verify", "clear", "check", "failure", "mean_error"]

IMAGES = {"png", "exr", "jpg", "jpeg", "tif", "tiff", "webp"}


def _ext(path: str) -> str:
    return os.path.splitext(path)[1].lower().lstrip(".")


def verify(files: list) -> str | None:
    """None when this machine's copy of each eval file ([path, sha256]) is the manager's; else
    the first that isn't."""
    for path, digest in files:
        name = "/".join(str(path).replace("\\", "/").split("/")[-3:])
        h = hashlib.sha256()
        try:
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
        except OSError:
            return f"{name} isn't in its checkout"
        if h.hexdigest() != digest:
            return f"its checkout's {name} isn't the manager's"
    return None


def clear(folder: str, scratch: str) -> None:
    """Empty the eval's folder before it runs. Only ever a folder inside <scratch>/evals."""
    root = os.path.normcase(os.path.abspath(os.path.join(scratch, "evals")))
    f = os.path.normcase(os.path.abspath(folder))
    if f == root or os.path.commonpath([root, f]) != root:
        raise ValueError(f"{folder} isn't a folder in this machine's evals folder ({root})")
    shutil.rmtree(f, ignore_errors=True)


def mean_error(output: str, expected: str, oiio=True) -> tuple[float | None, str | None]:
    """The mean difference between two images, per channel, over the channels both have:
    (error, None), or (None, why it can't be measured)."""
    mod = None
    if oiio:
        try:
            import OpenImageIO as mod  # noqa: N813
        except ImportError:
            mod = None
    if mod is not None:
        a, b = mod.ImageBuf(output), mod.ImageBuf(expected)
        sa, sb = a.spec(), b.spec()
        if a.has_error or b.has_error or not sa.width or not sb.width:
            return None, f"can't read it or the reference: {a.geterror() or b.geterror() or 'no image'}"
        if (sa.width, sa.height) != (sb.width, sb.height):
            return None, f"it is {sa.width} x {sa.height}, the reference {sb.width} x {sb.height}"
        n = min(sa.nchannels, sb.nchannels)
        if sa.nchannels > n:
            a = mod.ImageBufAlgo.channels(a, tuple(range(n)))
        if sb.nchannels > n:
            b = mod.ImageBufAlgo.channels(b, tuple(range(n)))
        return float(mod.ImageBufAlgo.compare(a, b, 1.0, 1.0).meanerror), None
    if _ext(output) != "png" or _ext(expected) != "png":
        return None, ("OpenImageIO's Python module isn't installed on this machine, so only PNGs can be compared "
                      "(pip install -i https://pypi.org/simple OpenImageIO)")
    try:
        x, y = png_pixels(output), png_pixels(expected)
    except (OSError, ValueError) as e:
        return None, f"can't read it or the reference: {e}"
    if x is None or y is None:
        return None, "a PNG the farm can't read without OpenImageIO (palette, interlaced or under 8 bits)"
    (w1, h1, c1, f1, p1), (w2, h2, c2, f2, p2) = x, y
    if (w1, h1) != (w2, h2):
        return None, f"it is {w1} x {h1}, the reference {w2} x {h2}"
    if (c1 < 3) != (c2 < 3):
        return None, "one is grey and the other colour"
    n = min(c1, c2)
    total = 0.0
    for i in range(w1 * h1):
        for c in range(n):
            total += abs(p1[i * c1 + c] / f1 - p2[i * c2 + c] / f2)
    return total / (w1 * h1 * n), None


def check(checks: dict, wrote: list[str], oiio=True) -> dict:
    """The checks on what the run wrote: {ok, checks: [{check, ok, said}]}."""
    out = []
    names = [os.path.basename(f) for f in wrote]
    for ext, want in (checks.get("files") or {}).items():
        n = sum(1 for f in wrote if _ext(f) == ext.lower())
        out.append({"check": f"files .{ext}", "ok": n == want, "said": f"{n} written, {want} wanted"})
    if checks.get("size"):
        w, h = checks["size"]
        images = [f for f in wrote if _ext(f) in IMAGES]
        wrong = [(f, image_size(f)) for f in images]
        wrong = [(f, s) for f, s in wrong if s != (w, h)]
        if not images:
            out.append({"check": "size", "ok": False, "said": "it wrote no image"})
        elif wrong:
            f, s = wrong[0]
            more = f" (and {len(wrong) - 1} more)" if len(wrong) > 1 else ""
            seen = f"{s[0]} x {s[1]}" if s else "unreadable"
            out.append({"check": "size", "ok": False,
                        "said": f"{os.path.basename(f)} is {seen}, wanted {w} x {h}{more}"})
        else:
            out.append({"check": "size", "ok": True, "said": f"{len(images)} image(s), each {w} x {h}"})
    for x in checks.get("like") or []:
        pattern, ref, most = x["output"], x["expected"], float(x["max_error"])
        hits = [f for f, n in zip(wrote, names) if fnmatch.fnmatch(n, pattern)]
        label = f"like {pattern}"
        if len(hits) != 1:
            out.append({"check": label, "ok": False,
                        "said": f"{len(hits)} files it wrote match {pattern}: it needs to match one"})
            continue
        err, why = mean_error(hits[0], ref, oiio=oiio)
        if why:
            out.append({"check": label, "ok": False, "said": f"{os.path.basename(hits[0])}: {why}"})
            continue
        out.append({"check": label, "ok": err <= most,
                    "said": f"{os.path.basename(hits[0])} against {os.path.basename(ref)}: mean error {err:.4f}, "
                            f"at most {most:g}"})
    return {"ok": all(c["ok"] for c in out), "checks": out}


def failure(result: dict) -> str:
    """A failed eval's error: the checks that didn't pass, then those that did."""
    bad = [c for c in result["checks"] if not c["ok"]]
    good = [c for c in result["checks"] if c["ok"]]
    text = "The eval failed: " + "; ".join(f"{c['check']}: {c['said']}" for c in bad) + "."
    if good:
        text += " Passed: " + "; ".join(f"{c['check']}: {c['said']}" for c in good) + "."
    return text
