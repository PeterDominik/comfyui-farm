"""The repo's own notes stay usable: every decision record is numbered, has the same
sections and is listed in the index, and CLAUDE.md names commands and files that exist."""

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DECISIONS = REPO / "docs" / "decisions"
SECTIONS = ["## Context", "## Decision", "## Consequences"]


class DecisionRecordTests(unittest.TestCase):
    def records(self) -> list[Path]:
        return sorted(p for p in DECISIONS.glob("*.md") if p.name != "README.md")

    def test_records_are_numbered_without_gaps(self):
        numbers = [p.name[:4] for p in self.records()]
        self.assertTrue(numbers, "docs/decisions has no records")
        self.assertEqual(numbers, [f"{i:04d}" for i in range(1, len(numbers) + 1)])

    def test_each_record_has_the_same_shape(self):
        for p in self.records():
            with self.subTest(record=p.name):
                self.assertRegex(p.name, r"^\d{4}-[a-z0-9-]+\.md$")
                lines = p.read_text(encoding="utf-8").splitlines()
                self.assertTrue(lines[0].startswith(f"# {p.name[:4]}. "), lines[0])
                self.assertRegex(lines[2], r"^Date: \d{4}-\d{2}-\d{2}\. Status: (accepted|replaced by \d{4})\.$")
                self.assertEqual([ln for ln in lines if ln.startswith("## ")], SECTIONS)

    def test_the_index_lists_every_record_once(self):
        index = (DECISIONS / "README.md").read_text(encoding="utf-8")
        linked = re.findall(r"\]\((\d{4}-[a-z0-9-]+\.md)\)", index)
        self.assertEqual(linked, [p.name for p in self.records()])
        for p in self.records():
            title = p.read_text(encoding="utf-8").splitlines()[0].split(". ", 1)[1]
            self.assertIn(f"| {title} |", index, p.name)


class ClaudeNotesTests(unittest.TestCase):
    def test_paths_named_in_claude_md_exist(self):
        text = (REPO / "CLAUDE.md").read_text(encoding="utf-8")
        named = set(re.findall(r"`((?:comfyfarm|client|workflows|deploy|docs|tests|tools|\.github)/[\w./-]*)`", text))
        self.assertGreater(len(named), 10)
        missing = sorted(n for n in named if not (REPO / n).exists())
        self.assertEqual(missing, [])

    def test_the_plan_lists_phases_0_to_14(self):
        """Each once, in the table; phase 11 comes before 9 and 10 (23 November)."""
        plan = (REPO / "docs" / "plan.md").read_text(encoding="utf-8")
        phases = [int(n) for n in re.findall(r"^\| (\d+) ", plan, flags=re.M)]
        self.assertEqual(sorted(phases), list(range(15)))
        self.assertLess(phases.index(11), phases.index(9))


if __name__ == "__main__":
    unittest.main()
