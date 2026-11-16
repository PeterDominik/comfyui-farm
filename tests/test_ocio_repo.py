"""The farm's OCIO config (ocio/config.ocio) does what Nuke does with bri3's published
config: scene_linear <-> color_picking, the ACES Rec.709 output transform.

The first tests read the YAML, so they run without OpenColorIO. The rest run the real
conversions when PyOpenColorIO is installed (it is in the farm's venv): against the values
measured in Nuke, and against ocio/reference/config_1_0_v1.ocio pixel for pixel."""
import os
import tempfile
import unittest

import numpy as np
import yaml

from comfyfarm.config import OcioConfig
from comfyfarm.fileio.ocio import DATA, IMAGE, LINEAR, NUKE_RAMP, REPO, ocio_config_path

CONFIG = ocio_config_path(OcioConfig())
OCIO_DIR = REPO / "ocio"
REFERENCE = OCIO_DIR / "reference" / "config_1_0_v1.ocio"


class _Loader(yaml.SafeLoader):
    pass


def _tagged(loader, tag, node):
    if isinstance(node, yaml.MappingNode):
        return {"_type": tag, **loader.construct_mapping(node, deep=True)}
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_scalar(node)


_Loader.add_multi_constructor("", _tagged)


def load(path=CONFIG):
    with open(path, encoding="utf-8") as fh:
        return yaml.load(fh, Loader=_Loader)


def lut_files(node):
    """Every FileTransform src in a parsed config."""
    if isinstance(node, dict):
        out = {node["src"]} if node.get("_type") == "FileTransform" else set()
        for v in node.values():
            out |= lut_files(v)
        return out
    if isinstance(node, list):
        return set().union(*(lut_files(v) for v in node)) if node else set()
    return set()


class RepoConfigTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load()
        self.spaces = {cs["name"]: cs for cs in self.cfg["colorspaces"]}

    def test_default_config_is_the_repo_one(self):
        self.assertEqual(os.path.normcase(CONFIG), os.path.normcase((OCIO_DIR / "config.ocio").as_posix()))

    def test_roles_and_old_names(self):
        roles = self.cfg["roles"]
        self.assertEqual({r: roles[r] for r in (LINEAR, IMAGE, DATA, "linear")},
                         {LINEAR: "acescg", IMAGE: "comfy_png", DATA: "raw", "linear": "acescg"})
        self.assertTrue(self.spaces["raw"]["isdata"])
        self.assertFalse(self.spaces["comfy_png"]["isdata"])
        # The names Rodeo's config and Nuke scripts use still resolve.
        aliases = {a: cs for cs, d in self.spaces.items() for a in d.get("aliases", [])}
        self.assertEqual((aliases["rdo"], aliases["ACES-look"], aliases["Utility - Raw"]), ("acescg", "comfy_png", "raw"))

    def test_luts_folder_holds_exactly_the_files_used(self):
        self.assertEqual(self.cfg["search_path"], "luts")
        used = lut_files(self.cfg["colorspaces"])
        self.assertEqual(used, {"Log2_48_nits_Shaper_to_linear.spi1d", "Log2_48_nits_Shaper.RRT.Rec.709.spi3d",
                                "InvRRT.Rec.709.Log2_48_nits_Shaper.spi3d"})
        self.assertEqual(set(os.listdir(OCIO_DIR / "luts")), used)

    def test_reference_uses_the_same_luts(self):
        ref = {cs["name"]: cs for cs in load(REFERENCE)["colorspaces"]}
        self.assertEqual(load(REFERENCE)["roles"][IMAGE], "ACES-look")
        self.assertEqual(lut_files(ref["ACES-look"]) - {"white_balance.cc"}, lut_files(self.spaces["comfy_png"]))


try:
    import PyOpenColorIO as OCIO
except ImportError:
    OCIO = None


def _apply(proc, px):
    a = np.ascontiguousarray(px, dtype=np.float32).copy()
    proc.applyRGB(a)
    return a


@unittest.skipUnless(OCIO, "needs PyOpenColorIO")
class RepoConfigProcessorTests(unittest.TestCase):
    """The conversions themselves, as oiiotool runs them on the farm."""

    def setUp(self):
        self.env = {k: os.environ.pop(k) for k in ("SEQ", "SHOT", "OCIO", "RDO_CURRENT_SHOW") if k in os.environ}
        self.addCleanup(os.environ.update, self.env)
        self.cfg = OCIO.Config.CreateFromFile(CONFIG)

    def cpu(self, cfg, src, dst):
        return cfg.getProcessor(src, dst).getDefaultCPUProcessor()

    def test_valid(self):
        self.cfg.validate()

    def test_matches_values_measured_in_nuke(self):
        fwd = self.cpu(self.cfg, LINEAR, IMAGE)
        self.assertEqual({v: round(fwd.applyRGB([v, v, v])[1] * 255) for v in NUKE_RAMP}, NUKE_RAMP)

    def test_round_trip(self):
        fwd, inv = self.cpu(self.cfg, LINEAR, IMAGE), self.cpu(self.cfg, IMAGE, LINEAR)
        for v in (0.01, 0.18, 0.5, 1.0):
            back = inv.applyRGB(fwd.applyRGB([v, v, v]))
            for b in back:
                self.assertAlmostEqual(b, v, delta=max(0.002, 0.01 * v))

    def test_data_is_untouched(self):
        self.assertEqual(self.cpu(self.cfg, DATA, LINEAR).applyRGB([0.25, 0.5, 0.75]), [0.25, 0.5, 0.75])

    def test_same_as_the_published_config(self):
        """Pixel for pixel against reference/config_1_0_v1.ocio, using the repo's LUTs plus a
        white_balance.cc: identity, and a strong one to show it cancels for any shot."""
        rng = np.random.default_rng(7)
        lin = np.concatenate([rng.random((60000, 3)) ** 3 * 1.5,
                              np.repeat(np.geomspace(1e-4, 16, 500)[:, None], 3, 1)]).astype(np.float32)
        png = rng.integers(0, 256, (60000, 3)).astype(np.float32) / 255
        ours_fwd = np.round(np.clip(_apply(self.cpu(self.cfg, LINEAR, IMAGE), lin), 0, 1) * 255)
        ours_inv = _apply(self.cpu(self.cfg, IMAGE, LINEAR), png)
        wbs = {"identity": ("1 1 1", "1"), "strong": ("1.25 0.95 0.8", "0.9")}
        for name, (slope, sat) in wbs.items():
            with self.subTest(white_balance=name), tempfile.TemporaryDirectory() as d:
                with open(f"{d}/white_balance.cc", "w") as fh:
                    fh.write(f'<ColorCorrection id="white_balance"><SOPNode><Slope>{slope}</Slope>'
                             f'<Offset>0 0 0</Offset><Power>1 1 1</Power></SOPNode>'
                             f'<SatNode><Saturation>{sat}</Saturation></SatNode></ColorCorrection>\n')
                ref = OCIO.Config.CreateFromFile(str(REFERENCE))
                ref.clearSearchPaths()               # one at a time: a ":"-joined string splits C:\\... on Windows
                ref.addSearchPath(d)
                ref.addSearchPath(str(OCIO_DIR / "luts"))
                ref_fwd = np.round(np.clip(_apply(self.cpu(ref, LINEAR, IMAGE), lin), 0, 1) * 255)
                ref_inv = _apply(self.cpu(ref, IMAGE, LINEAR), png)
                self.assertLessEqual(np.abs(ours_fwd - ref_fwd).max(), 1)
                self.assertLess(np.mean(ours_fwd != ref_fwd), 1e-4)
                rel = np.abs(ours_inv - ref_inv) / np.maximum(np.abs(ref_inv), 1e-3)
                self.assertLess(rel.max(), 1e-3)
