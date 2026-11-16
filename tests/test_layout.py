"""The package's layering, checked by importing modules with everything they mustn't
need made unimportable:

- fileio uses the standard library only, so the farm's own ComfyUI nodes can import it
  in ComfyUI's Python (no pydantic, yaml or web stack there).
- the manager's farm and desk don't need the agent or the web stack, so the set and
  agent tests run without them and a remote agent can replace the in-process one.
- the agent doesn't need the manager or its web server: a worker machine runs the agent
  alone, talking to the manager over HTTP.
- the agent doesn't need a backend: it runs jobs through a runner (runners/base.py), and
  only the ComfyUI runner knows ComfyUI's client, its websocket and its environment.
"""

import ast
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FILEIO = ["errors", "paths", "tools", "media", "ocio", "inputs", "naming", "textures", "convert", "outputs", "scratch",
          "farmnodes", "wire"]
WEB = ["fastapi", "starlette", "uvicorn", "httpx", "websockets"]


def import_without(modules: list[str], blocked: list[str]) -> subprocess.CompletedProcess:
    code = textwrap.dedent(f"""
        import importlib, importlib.abc, sys
        BLOCKED = {blocked!r}

        def blocked(name):
            return any(name == b or name.startswith(b + ".") for b in BLOCKED)

        class Block(importlib.abc.MetaPathFinder):
            def find_spec(self, name, path=None, target=None):
                if blocked(name):
                    raise ImportError("blocked: " + name)

        sys.meta_path.insert(0, Block())
        for m in {modules!r}:
            importlib.import_module(m)
        leaked = sorted(n for n in sys.modules if blocked(n))
        assert not leaked, leaked
    """)
    return subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True)


class LayoutTests(unittest.TestCase):
    def test_fileio_needs_only_the_standard_library(self):
        r = import_without([f"comfyfarm.fileio.{m}" for m in FILEIO],
                           ["pydantic", "yaml", "numpy", "PIL", "OpenImageIO", "PyOpenColorIO", *WEB])
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_manager_farm_needs_neither_the_agent_nor_the_web_stack(self):
        r = import_without(["comfyfarm.manager.farm", "comfyfarm.protocol"], WEB)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = import_without(["comfyfarm.manager.farm"], ["comfyfarm.agent"])
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_desk_needs_no_web_stack(self):
        r = import_without(["comfyfarm.manager.desk"], WEB)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = import_without(["comfyfarm.manager.desk"], ["comfyfarm.agent"])
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_agent_needs_neither_the_manager_nor_its_web_server(self):
        r = import_without(["comfyfarm.agent.agent", "comfyfarm.agent.link"],
                           ["comfyfarm.manager", "fastapi", "starlette", "uvicorn"])
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_agent_needs_no_backend(self):
        """Phase 1's gate (docs/plan.md): ComfyUI is behind the runner seam."""
        comfyui = ["comfyfarm.runners.comfyui", "comfyfarm.runners.comfy", "comfyfarm.comfyenv",
                   "comfyfarm.agent.launch", "comfyfarm.workflows", "httpx", "websockets"]
        r = import_without(["comfyfarm.agent.agent", "comfyfarm.runners", "comfyfarm.runners.base"], comfyui)
        self.assertEqual(r.returncode, 0, r.stderr)
        tree = ast.parse((REPO / "comfyfarm/agent/agent.py").read_text(encoding="utf-8"))
        imported = {(n.module or "") + "." + a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
                    for a in n.names} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        self.assertEqual(sorted(i for i in imported if any(w in i.lower() for w in ("comfy", "httpx", "websocket"))), [])

    def test_runners_need_neither_the_manager_nor_its_web_server(self):
        r = import_without(["comfyfarm.runners.comfyui"], ["comfyfarm.manager", "fastapi", "starlette", "uvicorn"])
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_example_configs_load_as_they_are(self):
        from comfyfarm.config import load_agent_config, load_config
        cfg = load_config(REPO / "config.example.yaml")
        self.assertEqual(cfg.obsolete_keys, [])
        self.assertEqual(cfg.agents.token, "")
        agent = load_agent_config(REPO / "agent.example.yaml")
        self.assertEqual((agent.comfyui, agent.max_inflight), ("http://127.0.0.1:8188", 2))
        self.assertEqual(agent.base_dir, REPO)

    def test_cli_help_needs_no_web_stack(self):
        r = subprocess.run([sys.executable, "-c", textwrap.dedent(f"""
            import importlib.abc, sys
            class Block(importlib.abc.MetaPathFinder):
                def find_spec(self, name, path=None, target=None):
                    if name.split(".")[0] in {WEB!r}:
                        raise ImportError("blocked: " + name)
            sys.meta_path.insert(0, Block())
            sys.argv = ["comfyfarm", "--help"]
            from comfyfarm.cli import main
            main()
        """)], cwd=REPO, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("standalone", r.stdout)


if __name__ == "__main__":
    unittest.main()
