"""Stand-in for oiiotool in tests: writes a tiny PNG to the -o path and logs what it
was asked to do (arguments and the OCIO context variables) as JSON lines.

Input file name containing 'broken' -> fails; containing 'slow' -> takes 1.5 s.
"""

import json
import os
import struct
import sys
import time
import zlib


def tiny_png() -> bytes:
    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\x80")) + chunk(b"IEND", b""))


args = sys.argv[1:]
if args[:1] == ["--buildinfo"]:
    print("OpenImageIO 3.0.0 (fake)\nDependencies: OpenColorIO 2.4.1 (fake)")
    sys.exit(0)
src = next(a for a in args if a.endswith(".exr"))
out = args[args.index("-o") + 1]
log = os.environ.get("FAKE_OIIOTOOL_LOG")
if log:
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"args": args, "SEQ": os.environ.get("SEQ"), "SHOT": os.environ.get("SHOT")}) + "\n")
if "broken" in os.path.basename(src):
    print(f"oiiotool ERROR: Could not open \"{src}\": bad EXR header", file=sys.stderr)
    sys.exit(1)
if "slow" in os.path.basename(src):
    time.sleep(1.5)
with open(out, "wb") as fh:
    fh.write(tiny_png())
