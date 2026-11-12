"""The farm's ComfyUI environment (comfyfarm.comfyenv): reading a machine, the
environment file, and how a machine differs from it."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from comfyfarm.comfyenv import cli as envcli
from comfyfarm.comfyenv import machine as M
from comfyfarm.comfyenv import spec as S

COMMIT_A = "a" * 40
COMMIT_B = "b" * 40


def write(path: Path, text: str = "", newline: str = "\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline=newline)
    return path


def git(folder: Path, commit: str, url: str = "https://github.com/x/y", packed: bool = False) -> None:
    write(folder / ".git" / "HEAD", "ref: refs/heads/main\n")
    if packed:
        write(folder / ".git" / "packed-refs", f"# pack-refs\n{commit} refs/heads/main\n")
    else:
        write(folder / ".git" / "refs" / "heads" / "main", commit + "\n")
    write(folder / ".git" / "config", f'[core]\n\tbare = false\n[remote "origin"]\n\turl = {url}\n')


class Farm:
    """A ComfyUI install and the two libraries, in a temp folder."""

    def __init__(self, root: Path):
        self.root = root
        self.bank = root / "bank" / "comfyui"
        self.farmlib = root / "bank" / "comfy_farm" / "comfyui"
        self.comfy = root / "ComfyUI"
        self.libraries = [{"path": str(self.bank)}, {"path": str(self.farmlib)}]
        write(self.bank / "models" / "diffusion_models" / "ltx.safetensors", "x" * 100)
        write(self.bank / "models" / "vae" / "ltx-vae.safetensors", "v" * 10)
        write(self.bank / "models" / "loras" / "LTX" / "alpha.safetensors", "l" * 7)
        write(self.farmlib / "models" / "checkpoints" / "chord_v1.safetensors", "c" * 5)
        write(self.farmlib / "custom_nodes" / "ComfyUI-Chord" / "__init__.py", "NODE_CLASS_MAPPINGS = {}\n")
        write(self.comfy / "comfyui_version.py", '__version__ = "0.33.0"\n')
        git(self.comfy, COMMIT_A, "https://github.com/comfyanonymous/ComfyUI")
        nodes = self.comfy / "custom_nodes"
        write(nodes / "ComfyUI-LTXVideo" / "__init__.py", "from .nodes import *\n")
        write(nodes / "ComfyUI-LTXVideo" / "nodes.py", "X = 1\n")
        git(nodes / "ComfyUI-LTXVideo", COMMIT_B, "https://github.com/Lightricks/ComfyUI-LTXVideo")
        write(nodes / "comfyui-kjnodes" / "__init__.py", "Y = 2\n")
        write(nodes / "comfyui-kjnodes" / "pyproject.toml",
              '[project]\nname = "comfyui-kjnodes"\nversion = "1.1.9"\n'
              '[project.urls]\nRepository = "https://github.com/kijai/ComfyUI-KJNodes"\n')
        write(nodes / "comfyui-kjnodes" / ".tracking", "")
        write(nodes / "my-own-pack" / "__init__.py", "")
        write(nodes / "websocket_image_save.py", "")
        write(nodes / "example_node.py.example", "")
        write(nodes / "comfyfarm_nodes" / "__init__.py", "")
        write(nodes / "__pycache__" / "x.pyc", "")
        self.model_paths()

    def model_paths(self, bank_section: str | None = None) -> None:
        bank = bank_section if bank_section is not None else (
            f"other_ui:\n  base_path: {self.bank}\n  is_default: false\n"
            "  checkpoints: models/checkpoints/\n  diffusion_models: |\n    models/diffusion_models\n    models/unet\n"
            "  loras: models/loras/\n  vae: models/vae/\n")
        write(self.comfy / "extra_model_paths.yaml", bank +
              f"comfyfarm:\n  base_path: {self.farmlib}\n  is_default: false\n  custom_nodes: custom_nodes/\n"
              "  checkpoints: models/checkpoints/\n  loras: models/loras/\n")

    def farm_file(self) -> list[dict]:
        """The farm's extra_model_paths.yaml, with this farm's libraries."""
        f = write(self.root / "farm_paths.yaml",
                  f"other_ui:\n  base_path: {self.bank.as_posix()}/\n  is_default: false\n"
                  "  checkpoints: models/checkpoints/\n  diffusion_models: |\n    models/diffusion_models\n"
                  "    models/unet\n  loras: models/loras/\n  vae: models/vae/\n"
                  f"comfyfarm:\n  base_path: {self.farmlib.as_posix()}/\n  is_default: false\n"
                  "  custom_nodes: custom_nodes/\n  checkpoints: models/checkpoints/\n  loras: models/loras/\n")
        return S.farm_model_paths(f)            # C:/… on Windows, where libraries list C:\…: still the same


STATS = {"system": {"comfyui_version": "0.33.0", "pytorch_version": "2.7.1+cu128",
                    "argv": ["main.py", "--listen", "0.0.0.0", "--use-sage-attention", "--fast", "fp16_accumulation"]}}

# A farm with two workflows: the LTX one uses LTXVideo, kjnodes and core nodes, CHORD the shared pack.
REGISTRY = SimpleNamespace(workflows={
    "ltx": SimpleNamespace(template={
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "ltx.safetensors"}},
        "2": {"class_type": "VAELoader", "inputs": {"vae_name": "ltx-vae.safetensors"}},
        "3": {"class_type": "LTXICLoRALoaderModelOnly", "inputs": {"lora_name": "LTX\\alpha.safetensors", "model": ["1", 0]}},
        "4": {"class_type": "WanVideoNAG", "inputs": {"strength": 1.0}},
        "5": {"class_type": "ComfyFarmLoadImages", "inputs": {"path": ""}}}),
    "chord": SimpleNamespace(template={
        "1": {"class_type": "ChordLoadModel", "inputs": {"ckpt_name": "chord_v1.safetensors"}},
        "2": {"class_type": "NotInstalledHere", "inputs": {}}}),
})
OBJECT_INFO = {
    "UNETLoader": {"python_module": "nodes"}, "VAELoader": {"python_module": "nodes"},
    "LTXICLoRALoaderModelOnly": {"python_module": "custom_nodes.ComfyUI-LTXVideo"},
    "WanVideoNAG": {"python_module": "custom_nodes.comfyui-kjnodes"},
    "ComfyFarmLoadImages": {"python_module": "custom_nodes.comfyfarm_nodes"},
    "ChordLoadModel": {"python_module": "custom_nodes.ComfyUI-Chord"},
}


class MachineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.farm = Farm(self.tmp)

    def test_launch_flags(self):
        self.assertEqual(M.launch_flags(STATS["system"]["argv"]), ["--fast fp16_accumulation", "--use-sage-attention"])
        self.assertEqual(M.launch_flags(["main.py", "--port", "8188", "--fast"]), ["--fast"])
        self.assertEqual(M.launch_flags(["--bf16-vae", "--fast=fp8_matrix_mult"]), ["--bf16-vae", "--fast fp8_matrix_mult"])

    def test_comfyui_folder_and_python_from_agent_yaml(self):
        launch = SimpleNamespace(command=["C:/py/python.exe", "-s", "ComfyUI/main.py", "--port", "8188"], cwd="D:/portable")
        cfg = SimpleNamespace(comfyui_dir="", comfyui_python="", comfyui_launch=launch)
        self.assertEqual(Path(M.comfyui_folder(cfg)), Path("D:/portable/ComfyUI"))
        self.assertEqual(M.comfyui_python(cfg), "C:/py/python.exe")
        cfg = SimpleNamespace(comfyui_dir="/x/ComfyUI", comfyui_python="/x/py", comfyui_launch=launch)
        self.assertEqual((M.comfyui_folder(cfg), M.comfyui_python(cfg)), ("/x/ComfyUI", "/x/py"))
        cfg = SimpleNamespace(comfyui_dir="", comfyui_python="",
                              comfyui_launch=SimpleNamespace(command=["conda", "run", "main.py"], cwd="/c"))
        self.assertEqual((M.comfyui_folder(cfg), M.comfyui_python(cfg)), ("/c", ""))     # not a python

    def test_git_heads(self):
        self.assertEqual(M.git_head(self.farm.comfy), {"commit": COMMIT_A, "url": "https://github.com/comfyanonymous/ComfyUI"})
        packed = self.tmp / "packed"
        git(packed, COMMIT_B, packed=True)
        self.assertEqual(M.git_head(packed)["commit"], COMMIT_B)
        detached = self.tmp / "detached"
        write(detached / ".git" / "HEAD", COMMIT_A + "\n")
        self.assertEqual(M.git_head(detached), {"commit": COMMIT_A, "url": None})
        self.assertIsNone(M.git_head(self.tmp / "nothing"))

    def test_source_hash_ignores_line_endings_and_caches(self):
        a, b = self.tmp / "a", self.tmp / "b"
        write(a / "x.py", "one\ntwo\n")
        write(b / "x.py", "one\ntwo\n", newline="\r\n")             # a Windows checkout
        write(b / "__pycache__" / "x.cpython-312.pyc", "junk")
        write(b / "web" / "x.js", "not python")
        self.assertEqual(M.source_hash(a), M.source_hash(b))
        write(b / "x.py", "one\nthree\n")
        self.assertNotEqual(M.source_hash(a), M.source_hash(b))

    def test_model_folders_in_comfyuis_order(self):
        mp = M.read_model_paths([str(self.farm.comfy / "extra_model_paths.yaml")])
        folders = M.search_paths(str(self.farm.comfy), mp)
        vae = folders["vae"]
        self.assertEqual(vae[0], os.path.normpath(self.farm.comfy / "models" / "vae"))      # its own first
        self.assertEqual(vae[1], os.path.normpath(self.farm.bank / "models" / "vae"))
        self.assertIn(os.path.normpath(self.farm.farmlib / "custom_nodes"), folders["custom_nodes"])
        # is_default puts a library first; legacy names count as their new ones
        write(self.tmp / "extra2.yaml", f"lib:\n  base_path: {self.farm.bank}\n  is_default: true\n  clip: models/clip\n")
        folders = M.search_paths(str(self.farm.comfy), M.read_model_paths([str(self.tmp / "extra2.yaml")]))
        self.assertEqual(folders["text_encoders"][0], os.path.normpath(self.farm.bank / "models" / "clip"))

    def test_collect(self):
        st = M.collect(str(self.farm.comfy), sys.executable, STATS, libraries=self.farm.libraries,
                       packs=["ComfyUI-LTXVideo", "ComfyUI-Chord"],
                       models={"ltx.safetensors": "diffusion_models", "LTX/alpha.safetensors": None,
                               "chord_v1.safetensors": "checkpoints", "gone.safetensors": "vae"},
                       packages=["pyyaml", "surely-not-installed-anywhere"])
        self.assertEqual(st["comfyui"]["version"], "0.33.0")
        self.assertEqual(st["comfyui"]["commit"], COMMIT_A)
        self.assertEqual(st["comfyui"]["flags"], ["--fast fp16_accumulation", "--use-sage-attention"])
        self.assertIsNone(st["python"]["packages"]["surely-not-installed-anywhere"])
        self.assertTrue(st["python"]["packages"]["pyyaml"])
        packs = st["packs"]
        self.assertNotIn("comfyfarm_nodes", packs)                  # the farm's own: versioned with the repo
        self.assertIn("websocket_image_save", packs)                # a single-file pack
        self.assertNotIn("__pycache__", packs)
        self.assertEqual(packs["ComfyUI-LTXVideo"][0]["commit"], COMMIT_B)
        self.assertTrue(packs["ComfyUI-LTXVideo"][0]["hash"])
        self.assertNotIn("hash", packs["my-own-pack"][0])           # only the farm's packs are hashed
        kj = packs["comfyui-kjnodes"][0]
        self.assertEqual((kj["version"], kj["registry"], kj["url"]), ("1.1.9", True, "https://github.com/kijai/ComfyUI-KJNodes"))
        self.assertTrue(packs["ComfyUI-Chord"][0]["shared"])
        self.assertFalse(packs["ComfyUI-LTXVideo"][0]["shared"])
        m = st["models"]
        self.assertEqual((m["ltx.safetensors"]["size"], m["ltx.safetensors"]["library"]), (100, str(self.farm.bank)))
        self.assertEqual(m["LTX/alpha.safetensors"]["folder"], "loras")     # found in whichever folder has it
        self.assertEqual(m["chord_v1.safetensors"]["library"], str(self.farm.farmlib))
        self.assertTrue(m["gone.safetensors"]["missing"])

    def test_unknown_folder(self):
        st = M.collect(str(self.tmp / "nope"))
        self.assertIn("doesn't exist", st["error"])
        self.assertIn("isn't known", M.collect("")["error"])
        self.assertEqual(S.compare({"comfyui": {}}, st)["notes"][-1], st["error"])


class SpecTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.farm = Farm(self.tmp)
        self.need = S.needs(REGISTRY, OBJECT_INFO)

    def state(self, **kw):
        w = S.wanted(self.spec) if hasattr(self, "spec") else {
            "libraries": self.farm.libraries, "packs": list(self.need["packs"]),
            "models": {m: None for m in self.need["models"]}, "packages": ["pyyaml"]}
        w.update(kw)
        st = M.collect(str(self.farm.comfy), sys.executable, STATS, **w)
        st["driver"] = "576.02"
        return st

    def snap(self):
        self.spec, warnings = S.snapshot(self.state(), self.need, "wrk59467w-5090", {"libraries": self.farm.libraries})
        return warnings

    def test_needs(self):
        self.assertEqual(self.need["packs"], {"ComfyUI-Chord": ["chord"], "ComfyUI-LTXVideo": ["ltx"],
                                              "comfyui-kjnodes": ["ltx"]})
        self.assertEqual(list(self.need["models"]), ["chord_v1.safetensors", "ltx-vae.safetensors", "ltx.safetensors",
                                                     "LTX/alpha.safetensors"])        # / on every OS
        self.assertEqual(self.need["unknown"], [("chord", "NotInstalledHere")])

    def test_the_farms_workflows(self):
        from comfyfarm.fileio.ocio import REPO
        from comfyfarm.workflows import WorkflowRegistry
        need = S.needs(WorkflowRegistry(REPO / "workflows"), {})
        self.assertIn("LTX/ltx-2.5-22b-ic-lora-alpha-gen-0.9.safetensors", need["models"])    # / on every OS
        self.assertIn("chord_v1.safetensors", need["models"])
        # No ComfyUI to say which pack has a node: only the packs the manifests name.
        self.assertEqual(need["packs"], {"ComfyUI-Comfy-Pro-Alpha-Generator": ["sam3_ltx_alpha"],
                                         "ComfyUI-LTXVideo": ["sam3_ltx_alpha"], "ComfyUI-SAM3": ["sam3_ltx_alpha"],
                                         "RES4LYF": ["wan22_i2i"]})

    def test_named_packs(self):
        """A pack a manifest names (node_packs) counts without a node of its own, and is
        recorded from the reference machine like the others."""
        registry = SimpleNamespace(workflows={**REGISTRY.workflows, "wan": SimpleNamespace(
            node_packs=["RES4LYF", "comfyui-kjnodes"],
            template={"1": {"class_type": "KSampler", "inputs": {"sampler_name": "res_2s"}},
                      "2": {"class_type": "WanVideoNAG", "inputs": {}}})})
        need = S.needs(registry, {**OBJECT_INFO, "KSampler": {"python_module": "nodes"}})
        self.assertEqual(need["packs"]["RES4LYF"], ["wan"])
        self.assertEqual(need["packs"]["comfyui-kjnodes"], ["ltx", "wan"])     # named and found: once per workflow
        warnings = S.snapshot(self.state(packs=list(need["packs"])), need, "ref")[1]
        self.assertIn("node pack RES4LYF (wan) isn't in this ComfyUI's custom_nodes folders", warnings)
        res = self.farm.comfy / "custom_nodes" / "RES4LYF"
        write(res / "__init__.py", "SAMPLERS = ['res_2s']\n")
        git(res, COMMIT_B, "https://github.com/ClownsharkBatwing/RES4LYF")
        spec, warnings = S.snapshot(self.state(packs=list(need["packs"])), need, "ref")
        self.assertEqual({k: spec["node_packs"]["RES4LYF"][k] for k in ("url", "commit", "for", "shared")},
                         {"url": "https://github.com/ClownsharkBatwing/RES4LYF", "commit": COMMIT_B, "for": ["wan"],
                          "shared": False})
        self.assertFalse(any("RES4LYF" in w for w in warnings))

    def test_the_environment_file_has_every_named_pack(self):
        """A pack a manifest names must be in the environment file: snapshot on the
        reference machine (wrk59467w-5090) records it."""
        from comfyfarm.fileio.ocio import REPO
        from comfyfarm.workflows import WorkflowRegistry
        need = S.needs(WorkflowRegistry(REPO / "workflows"), {})
        missing = sorted(set(need["packs"]) - set((S.load() or {}).get("node_packs") or {}))
        self.assertEqual(missing, [], "run `comfyfarm comfyui snapshot` on the reference machine and commit "
                                      "deploy/comfyui/environment.yaml")

    def test_snapshot(self):
        warnings = self.snap()
        spec = self.spec
        self.assertEqual(spec["reference"]["machine"], "wrk59467w-5090")
        self.assertEqual(spec["comfyui"], {"version": "0.33.0", "commit": COMMIT_A,
                                           "url": "https://github.com/comfyanonymous/ComfyUI",
                                           "flags": ["--fast fp16_accumulation", "--use-sage-attention"]})
        self.assertEqual(spec["python"], "%d.%d" % sys.version_info[:2])
        self.assertEqual(set(spec["node_packs"]), {"ComfyUI-Chord", "ComfyUI-LTXVideo", "comfyui-kjnodes"})
        self.assertEqual(spec["node_packs"]["ComfyUI-LTXVideo"]["commit"], COMMIT_B)
        self.assertEqual(spec["node_packs"]["comfyui-kjnodes"]["version"], "1.1.9")
        self.assertTrue(spec["node_packs"]["ComfyUI-Chord"]["shared"])
        self.assertEqual(spec["models"]["ltx.safetensors"], {"folder": "diffusion_models", "library": str(self.farm.bank),
                                                            "size": 100, "for": ["ltx"]})
        self.assertIn("chord: class NotInstalledHere isn't installed here", warnings)
        # written and read back
        S.save(spec, self.tmp / "env.yaml")
        text = (self.tmp / "env.yaml").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# The farm's ComfyUI"))
        self.assertEqual(S.load(self.tmp / "env.yaml"), spec)

    def test_driver_for_cuda(self):
        self.assertEqual(S.driver_for("2.7.1+cu128"), 570)
        self.assertEqual(S.driver_for("2.9.0+cu130"), 580)
        self.assertEqual(S.driver_for("2.5.1+cu124"), 550)
        self.assertIsNone(S.driver_for("2.7.1"))

    def test_os_specific_packages_are_kept_for_the_other_os(self):
        state = {"os": "windows", "python": {"packages": {"torch": "2.7.1+cu128", "triton-windows": "3.3.1.post19"}}}
        old = {"packages": {"triton": {"linux": "3.3.1"}, "triton-windows": {"windows": "3.2.0"}}}
        spec, _ = S.snapshot(state, {"packs": {}, "models": {}}, "w", old)
        self.assertEqual(spec["packages"], {"torch": "2.7.1+cu128", "triton": {"linux": "3.3.1"},
                                            "triton-windows": {"windows": "3.3.1.post19"}})
        self.assertEqual(spec["driver_min"], 570)

    def differs(self, state=None, farm_paths=None):
        return [d["message"] for d in S.compare(self.spec, state or self.state(), farm_paths)["differs"]]

    def test_the_reference_matches_itself(self):
        self.snap()
        self.assertEqual(self.differs(farm_paths=self.farm.farm_file()), [])

    def test_comfyui_flags_python_and_packages(self):
        self.snap()
        self.spec["packages"] = {"torch": "2.7.1+cu128", "sageattention": "2.2.0", "pyyaml": "1.0",
                                 "triton-windows": {"windows": "3.3.1"}, "triton": {"linux": "3.3.1"}}
        st = self.state()                   # reads the packages the file pins
        st["comfyui"].update(version="0.32.0", commit=COMMIT_B, flags=["--use-sage-attention"])
        st["python"]["python"] = "2.7.18"          # not the Python running the tests
        st["python"]["packages"].update({"torch": "2.7.1+cu126", "sageattention": "2.2.0+cu128torch2.7.1",
                                         "triton-windows": None, "triton": None})
        st["os"] = "linux"
        got = self.differs(st)
        self.assertIn(f"ComfyUI 0.32.0 / {COMMIT_B[:7]}; the farm's is 0.33.0 / {COMMIT_A[:7]}", got)
        self.assertIn("ComfyUI runs with --use-sage-attention; the farm's with --fast fp16_accumulation, "
                      "--use-sage-attention", got)
        self.assertIn(f"Python 2.7.18; the farm's is {self.spec['python']}", got)
        self.assertIn("torch 2.7.1+cu126; the farm's is 2.7.1+cu128", got)           # the CUDA build counts
        self.assertFalse([g for g in got if g.startswith("sageattention")])           # the same release
        self.assertIn("triton isn't installed; the farm's is 3.3.1", got)              # Linux's pin
        self.assertFalse([g for g in got if g.startswith("triton-windows")])          # Windows' only
        self.assertIn(f"pyyaml {st['python']['packages']['pyyaml']}; the farm's is 1.0", got)

    def test_driver(self):
        self.snap()
        self.spec["driver_min"] = 570
        st = self.state()
        st["driver"] = "566.36"
        self.assertIn("NVIDIA driver 566.36; torch", self.differs(st)[0])
        st["driver"] = None
        self.assertIn("couldn't read the NVIDIA driver version (nvidia-smi)", S.compare(self.spec, st)["notes"])

    def test_node_packs(self):
        self.snap()
        nodes = self.farm.comfy / "custom_nodes"
        write(nodes / "ComfyUI-LTXVideo" / "nodes.py", "X = 2\n")                    # edited in place
        shutil.rmtree(nodes / "comfyui-kjnodes")
        shutil.copytree(self.farm.farmlib / "custom_nodes" / "ComfyUI-Chord", nodes / "ComfyUI-Chord")   # a local copy too
        got = self.differs()
        local = os.path.normpath(nodes / "ComfyUI-LTXVideo")
        self.assertIn(f"Node pack ComfyUI-LTXVideo has local changes ({local})", got)
        self.assertIn("Node pack comfyui-kjnodes isn't installed (ltx use it)", got)
        self.assertTrue([g for g in got if g.startswith("Node pack ComfyUI-Chord is in 2 places")])
        self.assertTrue([g for g in got if g.startswith("Node pack ComfyUI-Chord should come from the shared library")])
        git(nodes / "ComfyUI-LTXVideo", COMMIT_A)
        self.assertIn(f"Node pack ComfyUI-LTXVideo is at {COMMIT_A[:7]}; the farm's is {COMMIT_B[:7]}", self.differs())

    def test_models(self):
        self.snap()
        write(self.farm.comfy / "models" / "vae" / "ltx-vae.safetensors", "local")       # shadows the library's
        write(self.farm.bank / "models" / "diffusion_models" / "ltx.safetensors", "y" * 99)
        os.remove(self.farm.bank / "models" / "loras" / "LTX" / "alpha.safetensors")
        got = self.differs()
        local = os.path.normpath(self.farm.comfy / "models" / "vae" / "ltx-vae.safetensors")
        self.assertIn(f"Model ltx-vae.safetensors comes from {local}, not from the farm's libraries", got)
        self.assertTrue([g for g in got if g.startswith("Model ltx.safetensors is 99 bytes here")])
        self.assertIn("Model LTX/alpha.safetensors isn't in ComfyUI's loras folders", got)

    def test_packs_and_models_of_workflows_a_machine_never_runs(self):
        """The 3090 never gets the LTX workflows (requires 32 GB): their packs and models
        don't matter there."""
        self.snap()
        shutil.rmtree(self.farm.comfy / "custom_nodes" / "ComfyUI-LTXVideo")
        os.remove(self.farm.bank / "models" / "loras" / "LTX" / "alpha.safetensors")
        self.assertEqual(len(self.differs()), 2)
        got = S.compare(self.spec, self.state(), skip={"ltx"})["differs"]
        self.assertEqual(got, [])
        reg = SimpleNamespace(workflows={"ltx": SimpleNamespace(requires={"vram_gb": 32, "tags": []}),
                                         "nightly": SimpleNamespace(requires={"vram_gb": None, "tags": ["nightly"]}),
                                         "wan": SimpleNamespace(requires={"vram_gb": None, "tags": []})})
        self.assertEqual(S.excluded(reg, 24 * 2**30, ["linux"]), {"ltx", "nightly"})
        self.assertEqual(S.excluded(reg, int(31.8 * 2**30), ["linux", "nightly"]), set())    # a 5090 counts as 32
        self.assertEqual(S.excluded(reg, None, []), {"nightly"})                             # VRAM unknown

    def test_extra_model_paths(self):
        self.snap()
        farm = self.farm.farm_file()
        self.farm.model_paths(f"other_ui:\n  base_path: {self.farm.bank}\n  is_default: false\n"
                              "  checkpoints: models/checkpoints/\n  diffusion_models: models/unet\n  vae: models/vae/\n"
                              f"local:\n  base_path: {self.tmp / 'elsewhere'}\n  checkpoints: models\n")
        got = [m for m in self.differs(farm_paths=farm) if m.startswith("extra_model_paths")]
        bank = str(Path(self.farm.bank.as_posix()))
        self.assertIn(f"extra_model_paths.yaml, {bank}: loras isn't listed; the farm's is models/loras/", got)
        self.assertIn(f"extra_model_paths.yaml, {bank}: diffusion_models is models/unet; the farm's is "
                      f"models/diffusion_models, models/unet", got)
        self.assertIn(f"extra_model_paths.yaml: local points at {self.tmp / 'elsewhere'}, which isn't one of the "
                      f"farm's libraries", got)
        self.farm.model_paths("")
        got = [m for m in self.differs(farm_paths=farm) if m.startswith("extra_model_paths")]
        self.assertEqual(got, [f"extra_model_paths.yaml doesn't list the farm library {bank}"])
        os.remove(self.farm.comfy / "extra_model_paths.yaml")
        self.assertIn("ComfyUI has no extra_model_paths.yaml: it doesn't see the farm's libraries",
                      self.differs(farm_paths=farm))

    @unittest.skipIf(os.name == "nt", "symlinks need extra rights on Windows")
    def test_a_library_under_another_name(self):
        """/shows/bank/comfyui is /rdo/bank/comfyui: the same library."""
        self.snap()
        alias = self.tmp / "shows_bank"
        os.symlink(self.farm.bank, alias)
        self.farm.model_paths(f"other_ui:\n  base_path: {alias}\n  is_default: false\n"
                              "  checkpoints: models/checkpoints/\n  diffusion_models: |\n    models/diffusion_models\n"
                              "    models/unet\n  loras: models/loras/\n  vae: models/vae/\n")
        self.assertEqual(self.differs(farm_paths=self.farm.farm_file()), [])

    def test_a_library_in_any_form(self):
        self.assertEqual(S._library("/rdo/bank/comfyui/", S.LIBRARIES), "/rdo/bank/comfyui")
        self.assertEqual(S._library("B:\\comfy_farm\\comfyui\\", S.LIBRARIES), "/rdo/bank/comfy_farm/comfyui")
        self.assertEqual(S._library("b:/comfyui", S.LIBRARIES), "/rdo/bank/comfyui")
        self.assertEqual(S._library("/elsewhere", S.LIBRARIES), "/elsewhere")

    def test_the_farm_file_in_the_repo(self):
        secs = S.farm_model_paths()
        self.assertEqual([s["library"] for s in secs], ["/rdo/bank/comfyui", "/rdo/bank/comfy_farm/comfyui"])
        self.assertEqual(secs[0]["given"]["loras"], ["models/loras/"])
        self.assertEqual(secs[1]["given"]["custom_nodes"], ["custom_nodes/"])
        self.assertEqual({lib["path"] for lib in S.LIBRARIES}, {s["library"] for s in secs})

    def test_cached(self):
        f = self.tmp / "env.yaml"
        c = S.Cached(f, self.tmp / "none.yaml")
        self.assertEqual(c.get(), (None, []))
        S.save({"comfyui": {"version": "1"}}, f)
        self.assertEqual(c.get()[0], {"comfyui": {"version": "1"}})


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.farm = Farm(self.tmp)
        self.out = self.tmp / "environment.yaml"
        self.args = ["--comfyui", str(self.farm.comfy), "--python", sys.executable, "--url", "http://127.0.0.1:1"]

    def run_cli(self, *argv):
        from comfyfarm.cli import main

        def get(url, timeout):
            return STATS if url.endswith("/system_stats") else OBJECT_INFO
        printed = []
        with mock.patch.object(envcli, "_get", get), \
                mock.patch.object(envcli.S, "SPEC", self.out), \
                mock.patch.object(envcli.S, "FARM_MODEL_PATHS", self.tmp / "farm_paths.yaml"), \
                mock.patch.object(envcli.S, "LIBRARIES", self.farm.libraries), \
                mock.patch("comfyfarm.workflows.WorkflowRegistry", lambda d: REGISTRY), \
                mock.patch("builtins.print", lambda *a, **k: printed.append(" ".join(map(str, a)))):
            code = main(["comfyui", *argv])
        return code, "\n".join(printed)

    def test_snapshot_then_verify(self):
        code, text = self.run_cli("verify", *self.args)
        self.assertEqual(code, 2)
        self.assertIn("There's no environment file yet", text)
        code, text = self.run_cli("snapshot", *self.args, "--name", "ref-5090")
        self.assertEqual(code, 0, text)
        self.assertIn("3 node pack(s), 4 model(s)", text)
        self.assertEqual(S.load(self.out)["reference"]["machine"], "ref-5090")
        self.farm.farm_file()
        code, text = self.run_cli("verify", *self.args)
        self.assertEqual(code, 0, text)
        self.assertIn("Matches the farm's ComfyUI.", text)
        write(self.farm.comfy / "custom_nodes" / "comfyui-kjnodes" / "__init__.py", "Y = 3\n")
        code, text = self.run_cli("verify", *self.args)
        self.assertEqual(code, 1)
        self.assertIn("1 difference(s):", text)
        kj = os.path.normpath(self.farm.comfy / "custom_nodes" / "comfyui-kjnodes")
        self.assertIn(f"Node pack comfyui-kjnodes has local changes ({kj})", text)
        code, text = self.run_cli("verify", *self.args, "--json")
        self.assertEqual(json.loads(text)["differs"][0]["item"], "comfyui-kjnodes")

    def test_agent_check_lines(self):
        cfg = SimpleNamespace(comfyui_dir=str(self.farm.comfy), comfyui_python=sys.executable, comfyui="http://x:1",
                              comfyui_launch=SimpleNamespace(command=[], cwd=""), resolve=Path)
        with mock.patch.object(envcli, "_get", lambda url, timeout: STATS), mock.patch.object(S, "SPEC", self.out), \
                mock.patch.object(S, "FARM_MODEL_PATHS", self.tmp / "none.yaml"):
            self.assertEqual(envcli.check_lines(cfg)[0][0], "WARN")          # no file yet
            S.save({"comfyui": {"version": "0.34.0"}}, self.out)
            self.assertEqual(envcli.check_lines(cfg), [
                ("WARN", f"differs from the farm's ComfyUI: ComfyUI 0.33.0 / {COMMIT_A[:7]}; the farm's is 0.34.0")])
            S.save({"comfyui": {"version": "0.33.0"}}, self.out)
            self.assertEqual(envcli.check_lines(cfg)[0][0], " ok ")


if __name__ == "__main__":
    unittest.main()
