"""Tests of scripts/changelog.py: python3 -m unittest discover -s scripts -p 'test_*.py'."""

import importlib.util
import sys
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location("changelog", Path(__file__).with_name("changelog.py"))
changelog = importlib.util.module_from_spec(_spec)
sys.modules["changelog"] = changelog  # dataclasses look the module up by name
_spec.loader.exec_module(changelog)

GOOD = """# Изменения

Вступление.

## [Unreleased]

## [v0.2.0] — 2026-11-02

### Несовместимо

- `Do` принимает `Request`.

## [v0.1.1] — 2026-10-25

- Исправлено.

## [v0.1.0] - 2026-10-20

- Первая версия.
"""


class ParseTest(unittest.TestCase):
    def test_sections_in_order_with_bodies(self):
        s = changelog.parse(GOOD)
        self.assertEqual([x.name for x in s], ["Unreleased", "v0.2.0", "v0.1.1", "v0.1.0"])
        self.assertEqual(s[0].body, "")
        self.assertEqual(s[1].date, "2026-11-02")
        self.assertEqual(s[1].body, "### Несовместимо\n\n- `Do` принимает `Request`.")
        self.assertEqual(s[3].date, "2026-10-20")  # a plain hyphen is accepted too

    def test_latest_and_notes(self):
        s = changelog.parse(GOOD)
        self.assertEqual(changelog.latest(s), "v0.2.0")
        self.assertEqual(changelog.notes(s, "v0.1.0"), "- Первая версия.")
        self.assertIsNone(changelog.notes(s, "v9.9.9"))

    def test_no_release_yet(self):
        s = changelog.parse("# Изменения\n\n## [Unreleased]\n\n- Черновик.\n")
        self.assertEqual(changelog.latest(s), "")
        self.assertEqual(changelog.problems(s), [])


class ProblemsTest(unittest.TestCase):
    def test_good_file(self):
        self.assertEqual(changelog.problems(changelog.parse(GOOD)), [])

    def assertProblem(self, text, fragment):
        found = changelog.problems(changelog.parse(text))
        self.assertTrue(any(fragment in p for p in found), f"{fragment!r} not in {found}")

    def test_unreleased_must_be_first(self):
        self.assertProblem("## [v0.1.0] — 2026-10-20\n\n- a\n", "first section")
        self.assertProblem("## [Unreleased]\n\n## [v0.1.0] — 2026-10-20\n\n- a\n\n## [Unreleased]\n", "must be the first")

    def test_bad_version_and_date(self):
        self.assertProblem("## [Unreleased]\n\n## [0.1.0] — 2026-10-20\n\n- a\n", "not a version")
        self.assertProblem("## [Unreleased]\n\n## [v0.1] — 2026-10-20\n\n- a\n", "not a version")
        self.assertProblem("## [Unreleased]\n\n## [v0.1.0]\n\n- a\n", "needs a date")
        self.assertProblem("## [Unreleased]\n\n## [v0.1.0] — 20.10.2026\n\n- a\n", "needs a date")

    def test_order_repeats_and_empty(self):
        self.assertProblem(
            "## [Unreleased]\n\n## [v0.1.0] — 2026-10-20\n\n- a\n\n## [v0.2.0] — 2026-10-21\n\n- b\n",
            "not older")
        self.assertProblem(
            "## [Unreleased]\n\n## [v0.1.0] — 2026-10-20\n\n- a\n\n## [v0.1.0] — 2026-10-20\n\n- a\n",
            "appears twice")
        self.assertProblem("## [Unreleased]\n\n## [v0.1.0] — 2026-10-20\n", "no entries")

    def test_minor_beats_patch_numerically(self):
        # v0.10.0 is newer than v0.9.0: compared as numbers, not strings.
        text = "## [Unreleased]\n\n## [v0.10.0] — 2026-10-21\n\n- b\n\n## [v0.9.0] — 2026-10-20\n\n- a\n"
        self.assertEqual(changelog.problems(changelog.parse(text)), [])


class MainTest(unittest.TestCase):
    def setUp(self):
        self.original = changelog.CHANGELOG
        tmp = Path(__file__).with_name("__pycache__") / "changelog-tests"
        tmp.mkdir(parents=True, exist_ok=True)
        changelog.CHANGELOG = tmp / "CHANGELOG.md"
        changelog.CHANGELOG.write_text(GOOD, encoding="utf-8")

    def tearDown(self):
        changelog.CHANGELOG = self.original

    def test_commands(self):
        self.assertEqual(changelog.main(["x", "latest"]), 0)
        self.assertEqual(changelog.main(["x", "notes", "v0.1.1"]), 0)
        self.assertEqual(changelog.main(["x", "notes", "v3.0.0"]), 1)
        self.assertEqual(changelog.main(["x", "check"]), 0)
        self.assertEqual(changelog.main(["x"]), 2)

    def test_check_fails_on_a_bad_file(self):
        changelog.CHANGELOG.write_text("## [v0.1.0]\n", encoding="utf-8")
        self.assertEqual(changelog.main(["x", "check"]), 1)

    def test_repository_changelog_is_valid(self):
        changelog.CHANGELOG = self.original
        self.assertEqual(changelog.main(["x", "check"]), 0)


if __name__ == "__main__":
    unittest.main()
