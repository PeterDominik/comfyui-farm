"""Compare the farm's colour conversions with OpenColorIO, pixel for pixel.

Run it on the farm machine, from the repo folder, in the gateway's venv:

    python tools\\ocio_compare.py
    python tools\\ocio_compare.py --exr D:\\test\\tile.1001.exr --nuke-png D:\\test\\tile_nuke.1001.png

Without files it converts a test ramp. With --exr and --nuke-png it also converts your
EXR with the farm's exact command and compares the result with Nuke's PNG (written with
"default (color_picking)") and with what OpenColorIO itself gives. Then it brings Nuke's
PNG back to linear with the farm's command and compares that with the EXR.
"""
import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "comfyfarm").is_dir())
sys.path.insert(0, str(ROOT))

import OpenImageIO as oiio  # noqa: E402
import PyOpenColorIO as OCIO  # noqa: E402

from comfyfarm import convert as C  # noqa: E402
from comfyfarm import textures as T  # noqa: E402
from comfyfarm.config import load_config  # noqa: E402


def read(path, fmt=oiio.FLOAT):
    buf = oiio.ImageBuf(str(path))
    px = buf.get_pixels(fmt)
    if buf.has_error or px is None:
        sys.exit(f"Can't read {path}: {buf.geterror()}")
    if px.ndim == 2:
        px = px[..., None]
    if px.shape[2] < 3:
        px = np.repeat(px[..., :1], 3, axis=2)
    return px[..., :3]


def write(path, px, fmt):
    h, w, n = px.shape
    buf = oiio.ImageBuf(oiio.ImageSpec(w, h, n, fmt))
    buf.set_pixels(oiio.ROI(0, w, 0, h, 0, 1, 0, n), np.ascontiguousarray(px))
    if not buf.write(str(path)):
        sys.exit(f"Can't write {path}: {buf.geterror()}")


def apply(proc, px):
    a = np.ascontiguousarray(px.reshape(-1, 3), dtype=np.float32).copy()
    proc.applyRGB(a)
    return a.reshape(px.shape)


def codes(px):
    return np.round(np.clip(px, 0, 1) * 255).astype(int)


def run(cmd, env):
    out = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if out.returncode:
        sys.exit(f"Failed: {' '.join(cmd)}\n{out.stderr or out.stdout}")
    return out


def by_level(label, ref_codes, others, names):
    """Mean of each image per 16-level band of the reference, all channels together."""
    print(f"   {label:>6s}  " + "  ".join(f"{n:>8s}" for n in names))
    r = ref_codes.reshape(-1)
    for b in range(0, 256, 16):
        sel = (r >= b) & (r < b + 16)
        if sel.sum() < 50:
            continue
        print(f"   {b:3d}-{b + 15:<3d} " + "  ".join(f"{o.reshape(-1)[sel].mean():8.1f}" for o in others))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="config.yaml (default: the repo's)")
    ap.add_argument("--exr", help="an EXR (linear) to convert")
    ap.add_argument("--nuke-png", help="the same image written by Nuke with default (color_picking)")
    a = ap.parse_args()

    cfg = load_config(a.config)
    oc = cfg.ocio
    ocio = C.ocio_setup(oc, cfg.paths)
    conv = dict(ocio, extra_args=list(cfg.convert.exr.extra_args))
    tool = C.require_tool(cfg.convert, "oiiotool")
    env = dict(os.environ, **ocio["context"])
    help_text = subprocess.run([*tool, "--help"], capture_output=True, text=True, env=env).stdout
    ver = subprocess.run([*tool, "--version"], capture_output=True, text=True, env=env).stdout.strip()

    print(f"oiiotool        {' '.join(tool)}  (OpenImageIO {ver})")
    for line in help_text.splitlines():
        if "OpenColorIO" in line or "Color config" in line or "olor configuration" in line:
            print(f"                {line.strip()}")
    print(f"PyOpenColorIO   {OCIO.__version__}")
    print(f"OCIO env var    {os.environ.get('OCIO') or '(not set)'}")
    print(f"config.yaml     ocio: config={oc.config} linear={oc.linear} image={oc.image} data={oc.data} "
          f"context={oc.context}")
    print(f"exr command     extra_args={conv['extra_args']}")
    print(f"config file     {ocio['config']}")

    ocfg = OCIO.Config.CreateFromFile(ocio["config"])
    ctx = ocfg.getCurrentContext()
    fwd = ocfg.getProcessor(ctx, oc.linear, oc.image).getDefaultCPUProcessor()
    inv = ocfg.getProcessor(ctx, oc.image, oc.linear).getDefaultCPUProcessor()
    print(f"OpenColorIO     {oc.linear} = {ocfg.getColorSpace(oc.linear).getName()}, "
          f"{oc.image} = {ocfg.getColorSpace(oc.image).getName()}")

    tmp = Path(tempfile.mkdtemp(prefix="ocio_compare_"))
    # ------------------------------------------------------------------ test ramp
    levels = [0, .002, .005, .01, .02, .03, .05, .08, .12, .18, .3, .5, .8, 1, 2, 4]
    vals = np.array([[v, v, v] for v in levels] + [[.8, .4, .1], [.1, .3, .6], [.5, .5, .2]], np.float32)
    vals = vals.astype(np.float16).astype(np.float32)          # what a half EXR holds
    ramp = np.repeat(np.repeat(vals[None], 8, 0), 8, 1)          # 8x8 px per value
    write(tmp / "ramp.exr", ramp, oiio.HALF)

    run(C.exr_command(tool, conv, str(tmp / "ramp.exr"), str(tmp / "ramp_farm.png")), env)
    farm = read(tmp / "ramp_farm.png", oiio.UINT8)[0, ::8].astype(int)
    want = codes(apply(fwd, vals))
    print("\n1. EXR -> PNG, test ramp: farm command vs OpenColorIO (8-bit codes R,G,B)")
    print("   linear                     OpenColorIO        farm           diff")
    for v, w_, f in zip(vals, want, farm):
        print(f"   {np.array2string(v, precision=3):24s} {str(w_):16s} {str(f):16s} {str(f - w_)}")
    print(f"   largest difference: {np.abs(farm - want).max()} levels")

    write(tmp / "ramp_ref.png", np.repeat(np.repeat(want[None].astype(np.uint8), 8, 0), 8, 1), oiio.UINT8)
    run(T.map_command(tool, ocio, {"color": True, "bits": "half"}, str(tmp / "ramp_ref.png"),
                      str(tmp / "ramp_back.exr")), env)
    back = read(tmp / "ramp_back.exr")[0, ::8]
    want_back = apply(inv, want / 255.0)
    print("\n2. PNG -> EXR (texture colour maps), the PNG above as Nuke writes it: farm vs OpenColorIO")
    print("   PNG code          OpenColorIO linear          farm linear")
    for c_, w_, f in zip(want, want_back, back):
        print(f"   {str(c_):16s} {np.array2string(w_, precision=4):26s} {np.array2string(f, precision=4)}")
    rel = np.abs(back - want_back) / np.maximum(want_back, 1e-3)
    print(f"   largest relative difference: {rel.max() * 100:.1f}%")

    print("\n   farm PNG attributes (a colour tag here could change how other apps read it):")
    info = subprocess.run([*tool, "--info", "-v", str(tmp / "ramp_farm.png")], capture_output=True, text=True,
                          env=env).stdout
    for line in info.splitlines():
        if any(k in line.lower() for k in ("color", "gamma", "icc", "srgb", "cicp")):
            print(f"     {line.strip()}")

    # ------------------------------------------------------------------ your files
    if a.exr:
        src = Path(a.exr)
        run([*tool, str(src), *conv["extra_args"], "-d", "float", "-o", str(tmp / "src.exr")], env)
        lin = read(tmp / "src.exr")
        run(C.exr_command(tool, conv, str(src), str(tmp / "farm.png")), env)
        farm = read(tmp / "farm.png", oiio.UINT8).astype(int)
        want = codes(apply(fwd, lin))
        mask = lin.max(axis=2) > 0.003          # skip the black gaps between UV islands
        print(f"\n3. {src.name} -> PNG: farm command vs OpenColorIO")
        d = (farm - want)[mask]
        print(f"   mean difference per channel {d.mean(0).round(2)}, mean |difference| {np.abs(d).mean(0).round(2)}")
        if a.nuke_png:
            nuke = read(a.nuke_png, oiio.UINT8).astype(int)
            if nuke.shape != farm.shape:
                sys.exit(f"Nuke's PNG is {nuke.shape[1]}x{nuke.shape[0]}, the farm's {farm.shape[1]}x{farm.shape[0]}")
            for name, img in (("farm", farm), ("OpenColorIO", want)):
                d = (img - nuke)[mask]
                print(f"   {name:11s} - Nuke: mean per channel {d.mean(0).round(2)}, "
                      f"mean |difference| {np.abs(d).mean(0).round(2)}")
            print("   mean PNG code per band of Nuke's code:")
            by_level("Nuke", nuke[mask], [nuke[mask], farm[mask], want[mask]], ["Nuke", "farm", "OCIO"])

            run(T.map_command(tool, ocio, {"color": True, "bits": "half"}, str(a.nuke_png), str(tmp / "nuke_back.exr")),
                env)
            nb = read(tmp / "nuke_back.exr")
            ob = apply(inv, nuke / 255.0)
            print(f"\n4. Nuke's PNG -> EXR with the farm command, compared with {src.name} (linear, per band):")
            print("   source linear   source      farm back   OpenColorIO back")
            s = lin[mask].reshape(-1)
            fb, ob_ = nb[mask].reshape(-1), ob[mask].reshape(-1)
            for lo, hi in ((0, .01), (.01, .03), (.03, .06), (.06, .1), (.1, .2), (.2, .4), (.4, .8), (.8, 2)):
                sel = (s >= lo) & (s < hi)
                if sel.sum() > 50:
                    print(f"   {lo:5.2f}-{hi:<5.2f}     {s[sel].mean():8.4f}   {fb[sel].mean():8.4f}   {ob_[sel].mean():8.4f}")
    print(f"\nFiles kept in {tmp}")


if __name__ == "__main__":
    main()
