"""Stand-in for oiiotool in tests: writes a tiny PNG to the -o path and logs what it
was asked to do (arguments and the OCIO context variables) as JSON lines.

Input file name containing 'broken' -> fails; containing 'slow' -> takes 1.5 s.
--ch behaves like the real one for PNG inputs: a channel the PNG doesn't have (greyscale
PNGs only have Y) gets the real warning, "filling with 0", and the call still succeeds.
FAKE_OIIOTOOL_LEVELS='{"<from>|<to>": grey, ...}' -> writes an 8x8 grey PNG at that
level, to simulate how each colour setting comes out.
"""

import json
import os
import struct
import sys
import time
import zlib

IMAGE_EXTENSIONS = {".exr", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"}


def tiny_png(grey: int = 128, size: int = 1) -> bytes:
    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    rows = b"".join(b"\x00" + bytes([grey]) * size for _ in range(size))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


args = sys.argv[1:]
if args[:1] == ["--buildinfo"]:
    print("OpenImageIO 3.0.0 (fake)\nDependencies: OpenColorIO 2.4.1 (fake)")
    sys.exit(0)
out = args[args.index("-o") + 1]
values = {i + 1 for i, a in enumerate(args) if a == "--colorconfig"}   # the config path isn't an input
src = next((a for i, a in enumerate(args) if i not in values and a != out and not a.startswith("-")
            and os.path.splitext(a)[1].lower() in IMAGE_EXTENSIONS), "")


def png_channels(path):
    try:
        with open(path, "rb") as fh:
            head = fh.read(26)
    except OSError:
        return None
    if head[:8] != b"\x89PNG\r\n\x1a\n" or len(head) < 26:
        return None
    return {0: ["Y"], 2: ["R", "G", "B"], 3: ["R", "G", "B"], 4: ["Y", "A"], 6: ["R", "G", "B", "A"]}.get(head[25])


warnings = []
have = png_channels(src) if src.lower().endswith(".png") else None
if have and "--ch" in args:
    for item in args[args.index("--ch") + 1].split(","):
        old = item.split("=", 1)[1] if "=" in item else item
        try:
            float(old)
            continue                     # a constant, or a channel index
        except ValueError:
            pass
        if old and old.upper() not in {h.upper() for h in have}:
            warnings.append(f'oiiotool WARNING: --ch: Unknown channel name "{old}", filling with 0 '
                            f'(actual channels: "{",".join(have)}")')
for w in warnings:
    print(w, file=sys.stderr)
log = os.environ.get("FAKE_OIIOTOOL_LOG")
if log:
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"args": args, "SEQ": os.environ.get("SEQ"), "SHOT": os.environ.get("SHOT"),
                             "warnings": warnings}) + "\n")
if "broken" in os.path.basename(src):
    print(f"oiiotool ERROR: Could not open \"{src}\": bad EXR header", file=sys.stderr)
    sys.exit(1)
if "slow" in os.path.basename(src):
    time.sleep(1.5)
levels = json.loads(os.environ.get("FAKE_OIIOTOOL_LEVELS") or "{}")
conv = args.index("--colorconvert") if "--colorconvert" in args else None
key = f"{args[conv + 1]}|{args[conv + 2]}" if conv is not None else ""
with open(out, "wb") as fh:
    fh.write(tiny_png(levels.get(key, 200), 8) if levels else tiny_png())
