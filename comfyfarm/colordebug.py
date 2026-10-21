"""`python -m comfyfarm color-debug`: find out why converted EXRs don't look right.

Converts one EXR frame exactly the way a job does, plus a small set of alternative
settings (input colour space, view, SHOT convention), and measures each result against
a reference frame you trust, typically the same frame of the shot's PNG preprocess
from the publish. It also reports what OCIO actually resolved (config, context, the
LUT files it found) and any colour tags in the PNGs, which some viewers apply.

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

ALT_INPUTS = ["ACES - ACES2065-1", "ACES - ACEScg", "rdo-nwb", "Input - ARRI - V3 LogC (EI800) - Wide Gamut"]
ALT_VIEWS = ["Rodeo-look", "Client-look"]


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
def ocio_report(c: dict, input_colorspace: str, view: str) -> list[str]:
    """What OCIO resolves with this context: the colour space behind the input name and
    the LUT files actually used. Needs the `opencolorio` package."""
    try:
        import PyOpenColorIO as OCIO
    except ImportError:
        return ["(install the 'opencolorio' package to see which LUT files OCIO resolves)"]
    saved = {k: os.environ.get(k) for k in c["context"]}
    os.environ.update(c["context"])   # same as a job: context comes from the environment
    try:
        cfg = OCIO.Config.CreateFromFile(c["config"])
        ctx = cfg.getCurrentContext()
        lines = [f"search path: {ctx.getSearchPath()}"]
        cs = cfg.getColorSpace(input_colorspace)
        lines.append(f"'{input_colorspace}' resolves to colour space: {cs.getName() if cs else 'NOT FOUND'}")
        lines.append(f"view '{view}' uses colour space: {cfg.getDisplayViewColorSpaceName(c['display'], view) or '?'}")
        for name in ("client_lut", "client_shot_grade", "white_balance.cc", "alt_grade.cc"):
            try:
                lines.append(f"  {name:<18} -> {ctx.resolveFileLocation(name)}")
            except Exception as e:  # noqa: BLE001
                lines.append(f"  {name:<18} -> not found ({str(e).splitlines()[0][:120]})")
        t = OCIO.DisplayViewTransform(src=input_colorspace, display=c["display"], view=view)
        proc = cfg.getProcessor(ctx, t, OCIO.TRANSFORM_DIR_FORWARD)
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
        inputs: list[str] | None, views: list[str] | None) -> int:
    import numpy as np  # noqa: F401  (fail early with a clear error if missing)
    pc, cc = cfg.paths, cfg.convert
    raw = exr if frame is None else f"{exr} {frame}-{frame}"
    seq = C.resolve_input(raw, pc, cc)
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
    say(f"OCIO config: {P.to_posix(c['config'], pc.mappings) or c['config']}")
    bundle = (P.to_posix(c["bundle"], pc.mappings) or c["bundle"]) if c["bundle"] else "none found (show defaults)"
    say(f"context:     {c['context']}   colour bundle: {bundle}")
    say(f"job uses:    from={c['input_colorspace']}  display={c['display']}  view={c['view']}  "
        f"extra={' '.join(c['extra_args'])}")
    say()
    say("What OCIO resolves for the job's settings:")
    for line in ocio_report(c, c["input_colorspace"], c["view"]):
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

    spaces, all_views = config_names(c["config"])
    in_list = inputs or [c["input_colorspace"]] + [s for s in ALT_INPUTS if s in spaces and s != c["input_colorspace"]]
    view_list = views or [c["view"]] + [v for v in ALT_VIEWS if v in all_views and v != c["view"]]
    contexts = [dict(c["context"])]
    if seq.shot and c["context"].get("SHOT") and c["context"]["SHOT"] != seq.shot:
        contexts.append(dict(c["context"], SHOT=seq.shot))   # the other SHOT convention

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
    for i, (cs, view, ctx) in enumerate(itertools.product(in_list, view_list, contexts)):
        tag = f"{i:02d}"
        dst = str(out / f"{tag}.png")
        cmd = C.exr_command(oiiotool, c, src, dst, input_colorspace=cs, view=view)
        res = subprocess.run(cmd, capture_output=True, text=True, env=dict(os.environ, **ctx), timeout=300)
        label = f"from={cs} | view={view} | SHOT={ctx.get('SHOT')}"
        if res.returncode != 0 or not os.path.isfile(dst):
            say(f"  {tag}  FAILED  {label}: {(res.stderr or res.stdout).strip()[-200:]}")
            continue
        img = read_rgb(dst)
        lv = levels(img)
        row = {"tag": tag, "label": label, "cs": cs, "view": view, "ctx": ctx, "levels": lv,
               "tags": png_color_tags(dst)}
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
                    "reference was made from this EXR, or what else the publish template does (grade, gamma, "
                    "a different config).")
        else:
            say(f"Best match: {best['tag']}  {best['label']}  (diff {best['cmp']['mae']:.1f}/255, "
                f"job settings {rows[0]['cmp']['mae']:.1f}/255)")
            say("To use it, set in config.yaml:")
            say("  convert:")
            say("    exr:")
            if best["cs"] != c["input_colorspace"]:
                say(f"      input_colorspace: '{best['cs']}'")
            if best["view"] != c["view"]:
                say(f"      view: '{best['view']}'")
            if best["ctx"] != c["context"]:
                say("      context: {SEQ: '{seq}', SHOT: '{shot}'}")
            say("What OCIO resolves for that variant:")
            for line in ocio_report(c | {"context": best["ctx"]}, best["cs"], best["view"]):
                say("  " + line)
    say()
    say(f"Images and this report: {out}")
    (out / "report.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
    return 0
