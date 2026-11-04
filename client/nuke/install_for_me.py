"""Adds the ComfyFarm panel to your own Nuke: one line in ~/.nuke/init.py pointing at this
folder. Run it once, from the farm's shared folder:

    python3 /rdo/bank/comfy_farm/client/nuke/install_for_me.py            # add
    python3 /rdo/bank/comfy_farm/client/nuke/install_for_me.py --remove   # take it out again

Restart Nuke afterwards. Safe to run again: the line is only added once. Standard library
only (any Python 3)."""

import os
import sys

MARK = "# ComfyFarm panel (install_for_me.py)"


def init_file() -> str:
    return os.path.join(os.path.expanduser("~"), ".nuke", "init.py")


def main(argv: list) -> int:
    here = os.path.dirname(os.path.abspath(__file__)).replace("\\", "/")
    line = f"import nuke; nuke.pluginAddPath({here!r})  {MARK}"
    path = init_file()
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except FileNotFoundError:
        lines = []
    kept = [ln for ln in lines if MARK not in ln]
    if "--remove" in argv:
        if kept == lines:
            print(f"Nothing to remove: {path} doesn't load the ComfyFarm panel.")
            return 0
        out = kept
        print(f"Removed the ComfyFarm panel from {path}. Restart Nuke.")
    else:
        if line in lines:
            print(f"Already installed: {path} loads {here}.")
            return 0
        out = kept + [line]            # replaces a line pointing somewhere else
        print(f"Added the ComfyFarm panel to {path} ({here}). Restart Nuke: it's under ComfyFarm in the menu bar.")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + ("\n" if out else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
