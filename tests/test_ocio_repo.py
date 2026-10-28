"""The OCIO config that ships in the repo (ocio/) has what the farm's conversions use:
the scene_linear, color_picking and data roles, and every LUT file on the way between
them, inside the repo. Parsed as YAML, so it runs without OpenColorIO; the last test runs
the real conversion when PyOpenColorIO is installed (it is in the farm's venv)."""
import os
import re
import unittest

import yaml

from comfyfarm.config import OcioConfig
from comfyfarm.convert import REPO, ocio_config_path

CONFIG = ocio_config_path(OcioConfig())


class _Loader(yaml.SafeLoader):
    pass


def _tagged(loader, tag, node):
    if isinstance(node, yaml.MappingNode):
        return {"_type": tag, **loader.construct_mapping(node, deep=True)}
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_scalar(node)


_Loader.add_multi_constructor("", _tagged)


def load():
    with open(CONFIG, encoding="utf-8") as fh:
        return yaml.load(fh, Loader=_Loader)


def colorspaces(cfg):
    return {cs["name"]: cs for cs in cfg["colorspaces"]}


def files_used(cfg, name, seen=None):
    """Every FileTransform src a colour space needs, through ColorSpaceTransforms too."""
    seen = set() if seen is None else seen
    if name in seen:
        return set()
    seen.add(name)
    css, out = colorspaces(cfg), set()

    def walk(t):
        if isinstance(t, list):
            for c in t:
                walk(c)
        elif isinstance(t, dict):
            if t.get("_type") == "FileTransform":
                out.add(t["src"])
            elif t.get("_type") == "ColorSpaceTransform":
                for k in ("src", "dst"):
                    out.update(files_used(cfg, cfg["roles"].get(t[k], t[k]), seen))
            walk(t.get("children", []))

    for key in ("to_reference", "from_reference"):
        walk(css[name].get(key))
    return out


@unittest.skipUnless(os.path.isfile(CONFIG), "ocio/config.ocio not in this checkout")
class RepoOcioConfigTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load()
        self.oc = OcioConfig()

    def test_default_config_is_the_repo_one(self):
        self.assertEqual(os.path.normcase(CONFIG), os.path.normcase(str(REPO / "ocio" / "config.ocio")))

    def test_roles_the_farm_uses(self):
        roles, css = self.cfg["roles"], colorspaces(self.cfg)
        for role in (self.oc.linear, self.oc.image, self.oc.data):
            self.assertIn(role, roles, f"no {role} role in ocio/config.ocio")
            self.assertIn(roles[role], css, f"{role} -> {roles[role]}, which isn't a colour space")
        # What Nuke's "default (color_picking)" and the texture names rely on.
        self.assertEqual(css[roles[self.oc.linear]].get("equalitygroup"), "ACEScg")
        self.assertTrue(css[roles[self.oc.data]].get("isdata"), "the data role must not change values")
        self.assertFalse(css[roles[self.oc.image]].get("isdata"))

    def test_luts_on_the_conversion_path_are_in_the_repo(self):
        dirs = [p for p in self.cfg["search_path"].split(":") if "$" not in p and not p.startswith("..")]
        self.assertTrue(dirs)
        for d in dirs:
            self.assertTrue((REPO / "ocio" / d).is_dir(), f"search_path folder ocio/{d} missing")
        roles = self.cfg["roles"]
        needed = files_used(self.cfg, roles[self.oc.linear]) | files_used(self.cfg, roles[self.oc.image])
        self.assertIn("white_balance.cc", needed)
        missing = [f for f in sorted(needed) if not any((REPO / "ocio" / d / f).is_file() for d in dirs)]
        self.assertEqual(missing, [], "LUT files the farm's conversions need but the repo doesn't have")

    def test_show_bundles_are_outside_the_repo(self):
        # The per-show _color_bundle folders are only found next to a show's config. The repo
        # copy falls back to project_luts, which doesn't change the result: color_picking and
        # scene_linear apply the same white_balance.cc in opposite directions.
        outside = [p for p in self.cfg["search_path"].split(":") if p.startswith("..")]
        self.assertTrue(all("_color_bundle" in p for p in outside))
        self.assertEqual(sorted(set(re.findall(r"\$\{(\w+)\}", self.cfg["search_path"]))), ["SEQ", "SHOT"])


try:
    import PyOpenColorIO as OCIO
except ImportError:
    OCIO = None


@unittest.skipUnless(OCIO and os.path.isfile(CONFIG), "needs PyOpenColorIO and ocio/config.ocio")
class RepoOcioProcessorTests(unittest.TestCase):
    """The conversions themselves, as oiiotool runs them on the farm (no SEQ/SHOT set)."""

    def setUp(self):
        self.env = {k: os.environ.pop(k) for k in ("SEQ", "SHOT", "OCIO") if k in os.environ}
        self.cfg = OCIO.Config.CreateFromFile(CONFIG)
        self.oc = OcioConfig()

    def tearDown(self):
        os.environ.update(self.env)

    def cpu(self, src, dst):
        return self.cfg.getProcessor(src, dst).getDefaultCPUProcessor()

    def test_round_trip(self):
        fwd, inv = self.cpu(self.oc.linear, self.oc.image), self.cpu(self.oc.image, self.oc.linear)
        for v in (0.0, 0.01, 0.18, 0.5, 1.0):
            img = fwd.applyRGB([v, v, v])
            self.assertTrue(all(0.0 <= c <= 1.0 for c in img), (v, img))
            back = inv.applyRGB(img)
            for b in back:
                self.assertAlmostEqual(b, v, delta=max(0.002, 0.01 * v))

    def test_mid_grey_is_toned_like_the_aces_srgb_view(self):
        # color_picking = ACES-look (ACES 1.x RRT + sRGB), not a plain sRGB curve: 0.18 lands
        # at 0.357 (PNG 91) rather than 0.46, and linear 1.0 at PNG 207.
        fwd = self.cpu(self.oc.linear, self.oc.image)
        self.assertAlmostEqual(fwd.applyRGB([0.18, 0.18, 0.18])[0], 0.357, delta=0.005)
        self.assertAlmostEqual(fwd.applyRGB([1.0, 1.0, 1.0])[0] * 255, 207, delta=1)

    def test_shot_folders_need_no_context(self):
        # ${SEQ}/${SHOT} unset, as on the farm: the search path still resolves.
        self.assertNotIn("SEQ", dict(self.cfg.getCurrentContext().getStringVars()))
        self.cfg.validate()

    def test_data_is_untouched(self):
        p = self.cpu(self.oc.data, self.oc.linear)
        self.assertEqual(p.applyRGB([0.25, 0.5, 0.75]), [0.25, 0.5, 0.75])
