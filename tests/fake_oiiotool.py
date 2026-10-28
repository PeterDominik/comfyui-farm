"""Stand-in for oiiotool in tests: writes a tiny PNG to the -o path and logs what it
was asked to do (arguments and the OCIO context variables) as JSON lines.

Input file name containing 'broken' -> fails; containing 'slow' -> takes 1.5 s.
FAKE_OIIOTOOL_LEVELS='{"<from>|<view>": grey, ...}' -> writes an 8x8 grey PNG at that
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
log = os.environ.get("FAKE_OIIOTOOL_LOG")
if log:
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"args": args, "SEQ": os.environ.get("SEQ"), "SHOT": os.environ.get("SHOT")}) + "\n")
if "broken" in os.path.basename(src):
    print(f"oiiotool ERROR: Could not open \"{src}\": bad EXR header", file=sys.stderr)
    sys.exit(1)
if "slow" in os.path.basename(src):
    time.sleep(1.5)
levels = json.loads(os.environ.get("FAKE_OIIOTOOL_LEVELS") or "{}")
disp = next((i for i, a in enumerate(args) if a.startswith("--ociodisplay")), None)
key = f"{args[disp].split('from=', 1)[1]}|{args[disp + 2]}" if disp is not None else ""
with open(out, "wb") as fh:
    fh.write(tiny_png(levels.get(key, 200), 8) if levels else tiny_png())
