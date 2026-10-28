"""`python -m comfyfarm color-debug`: find out why converted EXRs don't look right.

Converts one EXR frame exactly the way a job does (the repo's OCIO config, linear ->
color_picking, like a Nuke Write set to "default (color_picking)"), plus alternative
input and output colour spaces, and measures each result against a reference frame you
trust, typically the same frame of the shot's PNG preprocess from the publish. It also
reports what the names resolve to in the config, the LUT files the transform uses, and
any colour tags in the PNGs, which some viewers apply.

Run it on the farm machine:
    python -m comfyfarm color-debug /rdo/shows/raa/.../bpash_1545.plate.v1.%04d.exr --frame 1001 \\
        --reference /rdo/shows/raa/.../bpash_1545.preprocess.mp01_png.v1.1001.png
"""

from __future__ import annotations

import itertools
import os
import re
import struct
import subprocess
import time
from pathlib import Path

from . import convert as C
from . import paths as P
from .config import Config

# Tried as well, when the config has them: what else the EXR could be in, and what else
# the PNG could be meant to be in.
ALT_FROM = ["ACES - ACEScg", "ACEScg", "ACES - ACES2065-1", "ACES2065-1", "lin_rec709", "Utility - Linear - sRGB",
            "rdo-nwb"]
ALT_TO = ["Utility - sRGB - Texture", "srgb_tx", "sRGB - Texture", "Output - sRGB", "Utility - Raw", "Raw"]


# ------------------------------------------------------------------ images
def read_rgb(path: str):
    """float32 HxWx3 in 0..1 (pixel values as stored; colour tags ignored)."""
    import numpy as np
    try:
        import OpenImageIO as oiio
        buf = oiio.ImageBuf(path)
        if buf.has_error:
            raise RuntimeError(buf.geterror())
        px = buf.get_pixels(oiio.FLOAT)
        if px is None:
            raise RuntimeError(f"can't read {path}")
        px = np.asarray(px, dtype=np.float32)
    except ImportError:
        from PIL import Image
        px = np.asarray(Image.open(path).convert("RGB"), dtype=np.float32) / 255.0
    if px.ndim == 2:
        px = px[:, :, None]
    if px.shape[2] == 1:
        px = np.repeat(px, 3, axis=2)
    return px[:, :, :3]


def write_png(path: str, rgb) -> None:
    import numpy as np
    data = (np.clip(rgb, 0, 1) * 255 + 0.5).astype(np.uint8)
    try:
        import OpenImageIO as oiio
        out = oiio.ImageOutput.create(path)
        out.open(path, oiio.ImageSpec(data.shape[1], data.shape[0], 3, oiio.UINT8))
        out.write_image(data)
        out.close()
    except ImportError:
        from PIL import Image
        Image.fromarray(data).save(path)


def resample(img, h: int, w: int):
    """Nearest-neighbour resize, good enough for comparing levels."""
    import numpy as np
    ys = np.linspace(0, img.shape[0] - 1, h).round().astype(int)
    xs = np.linspace(0, img.shape[1] - 1, w).round().astype(int)
    return img[ys][:, xs]


def levels(img) -> dict:
    import numpy as np
    luma = img @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
    p1, p50, p99 = np.percentile(luma, [1, 50, 99])
    return {"black": float(p1), "median": float(p50), "white": float(p99),
            "mean_rgb": [float(v) for v in img.reshape(-1, 3).mean(axis=0)]}


def compare(ours, ref) -> dict:
    import numpy as np
    if ours.shape[:2] != ref.shape[:2]:
        ours = resample(ours, ref.shape[0], ref.shape[1])
    diff = np.abs(ours - ref)
    lo, lr = levels(ours), levels(ref)
    return {"mae": float(diff.mean() * 255), "black_shift": (lo["black"] - lr["black"]) * 255,
            "median_shift": (lo["median"] - lr["median"]) * 255, "white_shift": (lo["white"] - lr["white"]) * 255,
            "resized": ours.shape[:2] != ref.shape[:2]}


def png_color_tags(path: str) -> list[str]:
    """Colour chunks some viewers honour (gAMA, sRGB, iCCP, cICP, cHRM)."""
    tags = []
    try:
        with open(path, "rb") as fh:
            if fh.read(8) != b"\x89PNG\r\n\x1a\n":
                return ["not a PNG"]
            while True:
                head = fh.read(8)
                if len(head) < 8:
                    break
                length, kind = struct.unpack(">I4s", head)
                data = fh.read(length)
                fh.read(4)
                kind = kind.decode("latin-1")
                if kind == "gAMA":
                    tags.append(f"gAMA {struct.unpack('>I', data)[0] / 100000:.5f}")
                elif kind == "sRGB":
                    tags.append("sRGB")
                elif kind == "cICP":
                    tags.append(f"cICP primaries={data[0]} transfer={data[1]} matrix={data[2]}")
                elif kind in ("iCCP", "cHRM"):
                    tags.append(kind)
                elif kind == "IDAT":
                    break
    except OSError as e:
        return [f"unreadable: {e}"]
    return tags


# ------------------------------------------------------------------ OCIO introspection
def ocio_report(c: dict, from_cs: str, to_cs: str) -> list[str]:
    """What the names resolve to in the config, and the LUT files the conversion uses.
    Needs the `opencolorio` package."""
    try:
        import PyOpenColorIO as OCIO
    except ImportError:
        return ["(install the 'opencolorio' package to see what the names resolve to)"]
    saved = {k: os.environ.get(k) for k in c["context"]}
    os.environ.update(c["context"])   # same as a job: context comes from the environment
    try:
        cfg = OCIO.Config.CreateFromFile(c["config"])
        ctx = cfg.getCurrentContext()
        lines = [f"search path: {ctx.getSearchPath() or '(none)'}"]
        for name in (from_cs, to_cs):
            cs = cfg.getColorSpace(name)
            lines.append(f"'{name}' resolves to colour space: {cs.getName() if cs else 'NOT FOUND'}")
        proc = cfg.getProcessor(ctx, from_cs, to_cs)
        files = list(proc.getProcessorMetadata().getFiles())
        lines.append("files in the transform: " + (", ".join(files) if files else "(none)"))
        return lines
    except Exception as e:  # noqa: BLE001
        return [f"OCIO error: {e}"]
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def config_names(config_path: str) -> tuple[set[str], set[str]]:
    """Colour space names and view names in the config (text scan; tolerant of v1 tags)."""
    try:
        text = Path(config_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set(), set()
    spaces = set(re.findall(r"^\s*name:\s*(.+?)\s*$", text, re.MULTILINE))
    views = set(re.findall(r"\{\s*name:\s*([^,}]+?)\s*,", text))
    return spaces, views


# ------------------------------------------------------------------ main
def run(cfg: Config, exr: str, frame: int | None, reference: str | None, out_dir: str | None,
        from_list: list[str] | None = None, to_list: list[str] | None = None) -> int:
    import numpy as np  # noqa: F401  (fail early with a clear error if missing)
    pc, cc = cfg.paths, cfg.convert
    raw = exr if frame is None else f"{exr} {frame}-{frame}"
    seq = C.resolve_input(raw, pc, cc, ocio=cfg.ocio)
    if seq.kind != "exr":
        print("color-debug is for EXR inputs.")
        return 2
    c = seq.convert
    rx = P._pattern_regex(c["src_pattern"])
    src_name = c["src_pattern"]
    if rx:   # the file for the first frame of the range
        src_name = next(n for n in sorted(os.listdir(c["src_dir"]))
                        if rx.match(n) and int(rx.match(n).group(1)) == seq.first)
    src = c["src_dir"] + src_name
    out = Path(out_dir) if out_dir else cfg.resolve(f"data/color_debug/{time.strftime('%Y%m%d-%H%M%S')}")
    out.mkdir(parents=True, exist_ok=True)
    oiiotool = C.require_tool(cc, "oiiotool")
    report: list[str] = []

    def say(line: str = "") -> None:
        print(line)
        report.append(line)

    say(f"EXR frame:   {P.to_posix(src, pc.mappings) or src}")
    say(f"OCIO config: {c['config']}" + (f"   context: {c['context']}" if c["context"] else ""))
    say(f"job uses:    from={c['from']}  to={c['to']}  extra={' '.join(c['extra_args'])}")
    say()
    say("What OCIO resolves for the job's settings:")
    for line in ocio_report(c, c["from"], c["to"]):
        say("  " + line)

    # The EXR itself: is it really scene-linear?
    try:
        import OpenImageIO as oiio  # noqa: F401
        lin = read_rgb(src)
        lv = levels(lin)
        say()
        say(f"EXR levels (luma): 1%={lv['black']:.4f}  median={lv['median']:.4f}  99%={lv['white']:.4f}  "
            f"max={float(lin.max()):.3f}")
        if lin.max() <= 1.0 and lv["median"] > 0.3 and lv["black"] > 0.05:
            say("  -> values sit in 0..1 with raised blacks and a high median: this looks LOG-encoded or "
                "display-referred, not scene-linear. Treating it as linear lifts the result.")
    except ImportError:
        pass

    spaces, _views = config_names(c["config"])
    in_list = from_list or [c["from"]] + [s for s in ALT_FROM if s in spaces and s != c["from"]]
    out_list = to_list or [c["to"]] + [s for s in ALT_TO if s in spaces and s != c["to"]]

    ref = None
    if reference:
        ref_path = P.to_farm(reference, pc.mappings)
        ref = read_rgb(ref_path)
        lr = levels(ref)
        say()
        say(f"reference:   {reference}  ({ref.shape[1]}x{ref.shape[0]})  tags: {png_color_tags(ref_path) or 'none'}")
        say(f"  levels: black={lr['black'] * 255:.1f}  median={lr['median'] * 255:.1f}  white={lr['white'] * 255:.1f}  (0-255)")
        write_png(str(out / "reference.png"), ref)

    say()
    say("Variants (levels in 0-255; shifts are vs the reference, + = brighter):")
    rows = []
    env = dict(os.environ, **c["context"])
    for i, (src_cs, dst_cs) in enumerate(itertools.product(in_list, out_list)):
        tag = f"{i:02d}"
        dst = str(out / f"{tag}.png")
        cmd = C.exr_command(oiiotool, c, src, dst, from_cs=src_cs, to_cs=dst_cs)
        res = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=300)
        label = f"from={src_cs} | to={dst_cs}"
        if res.returncode != 0 or not os.path.isfile(dst):
            say(f"  {tag}  FAILED  {label}: {(res.stderr or res.stdout).strip()[-200:]}")
            continue
        img = read_rgb(dst)
        lv = levels(img)
        row = {"tag": tag, "label": label, "from": src_cs, "to": dst_cs, "levels": lv, "tags": png_color_tags(dst)}
        line = f"  {tag}  black={lv['black'] * 255:5.1f}  median={lv['median'] * 255:5.1f}  white={lv['white'] * 255:5.1f}"
        if ref is not None:
            cmp = compare(img, ref)
            row["cmp"] = cmp
            line += (f"  | diff={cmp['mae']:5.1f}  black {cmp['black_shift']:+5.1f}  median {cmp['median_shift']:+5.1f}"
                     f"  white {cmp['white_shift']:+5.1f}")
            import numpy as np
            same = img if img.shape[:2] == ref.shape[:2] else resample(img, ref.shape[0], ref.shape[1])
            # ours | reference | difference x4
            side = np.concatenate([same, ref, np.clip(np.abs(same - ref) * 4, 0, 1)], axis=1)
            write_png(str(out / f"{tag}_vs_reference.png"), side)
        say(line + f"   {label}" + ("   [job settings]" if i == 0 else ""))
        rows.append(row)

    if rows and rows[0]["tags"]:
        say()
        say(f"Colour tags in the converted PNGs: {', '.join(rows[0]['tags'])}. Viewers that honour these "
            f"(browsers, some image viewers) display the frames differently from Nuke/ComfyUI, which read raw values.")

    if ref is not None and rows:
        best = min(rows, key=lambda r: r["cmp"]["mae"])
        say()
        if best is rows[0]:
            say(f"Best match is the job's own settings (diff {best['cmp']['mae']:.1f}/255).")
            if best["cmp"]["mae"] > 4:
                say("It's still far off, so the difference is probably not in these settings: check that the "
                    "reference was made from this EXR, and what the Write that made it does (its transform, a "
                    "grade or look, a different config).")
        else:
            say(f"Best match: {best['tag']}  {best['label']}  (diff {best['cmp']['mae']:.1f}/255, "
                f"job settings {rows[0]['cmp']['mae']:.1f}/255)")
            say("To use it, set in config.yaml:")
            say("  ocio:")
            if best["from"] != c["from"]:
                say(f"    linear: '{best['from']}'")
            if best["to"] != c["to"]:
                say(f"    image: '{best['to']}'")
            say("What OCIO resolves for that variant:")
            for line in ocio_report(c, best["from"], best["to"]):
                say("  " + line)
    say()
    say(f"Images and this report: {out}")
    (out / "report.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
    return 0
