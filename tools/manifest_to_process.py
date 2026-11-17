"""Write a workflow's manifest.yaml as a process.yaml (docs/decisions/0015).

    python tools/manifest_to_process.py workflows/wan22_i2i            # prints it
    python tools/manifest_to_process.py workflows/wan22_i2i --write    # writes process.yaml beside it

The result loads as the same workflow: same parameters, same graph wiring. It is not
the same file, though: comments are not carried over, and the definition's fingerprint
changes only if you add head fields (version, class, targets, limits, models, eval).
With --write, remove manifest.yaml yourself once you have moved its comments: a folder
with both files does not load.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "comfyfarm").is_dir())
sys.path.insert(0, str(ROOT))


def convert(folder: Path) -> str:
    from comfyfarm.workflows import to_process
    m = yaml.safe_load((folder / "manifest.yaml").read_text(encoding="utf-8")) or {}
    return yaml.safe_dump(to_process(m), sort_keys=False, allow_unicode=True, width=100)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("folder", help="a workflow folder with a manifest.yaml")
    ap.add_argument("--write", action="store_true", help="write process.yaml into the folder")
    args = ap.parse_args(argv)
    folder = Path(args.folder)
    if not (folder / "manifest.yaml").exists():
        print(f"No manifest.yaml in {folder}.", file=sys.stderr)
        return 1
    text = convert(folder)
    if not args.write:
        print(text, end="")
        return 0
    out = folder / "process.yaml"
    if out.exists():
        print(f"{out} is already there: not overwritten.", file=sys.stderr)
        return 1
    out.write_text(text, encoding="utf-8")
    print(f"Written {out}. Move the comments over, then remove {folder / 'manifest.yaml'}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
