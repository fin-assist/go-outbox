"""Tests of scripts/coverage-diff.py: python3 -m unittest discover -s scripts -p 'test_*.py'."""

import importlib.util
import sys
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location("coverage_diff", Path(__file__).with_name("coverage-diff.py"))
cd = importlib.util.module_from_spec(_spec)
sys.modules["coverage_diff"] = cd  # dataclasses look the module up by name
_spec.loader.exec_module(cd)

MOD = "github.com/fin-assist/svc"

PROFILE = """mode: atomic
github.com/fin-assist/svc/internal/a/a.go:10.20,12.3 2 1
github.com/fin-assist/svc/internal/a/a.go:13.2,15.10 1 0
github.com/fin-assist/svc/internal/a/a.go:13.2,15.10 1 3
github.com/fin-assist/svc/internal/a/a.go:20.1,21.2 1 0
github.com/other/mod/x.go:1.1,2.2 1 1
"""

DIFF = """diff --git a/internal/a/a.go b/internal/a/a.go
--- a/internal/a/a.go
+++ b/internal/a/a.go
@@ -9,0 +10,2 @@ func A() {
+x
+y
@@ -19 +20 @@
-old
+new
@@ -30,2 +30,0 @@
-gone
-gone
diff --git a/internal/a/a_test.go b/internal/a/a_test.go
--- a/internal/a/a_test.go
+++ b/internal/a/a_test.go
@@ -1 +1,3 @@
+t
diff --git a/internal/b/old.go b/internal/b/old.go
--- a/internal/b/old.go
+++ /dev/null
@@ -1,3 +0,0 @@
-x
"""


class ProfileTest(unittest.TestCase):
    def test_lines_inside_blocks_and_merge_across_binaries(self):
        p = cd.parse_profile(PROFILE, MOD)
        a = p["internal/a/a.go"]
        self.assertTrue(a[10] and a[11] and a[12])
        self.assertTrue(a[14], "a block run by one test binary counts as covered")
        self.assertFalse(a[20])
        self.assertNotIn(16, a)
        self.assertEqual(set(p), {"internal/a/a.go"}, "files outside the module are skipped")


class DiffTest(unittest.TestCase):
    def test_changed_lines_of_production_files(self):
        self.assertEqual(cd.changed_lines(DIFF), {"internal/a/a.go": {10, 11, 20}})

    def test_testdata_is_not_production(self):
        self.assertFalse(cd.is_production("internal/x/testdata/y.go"))
        self.assertFalse(cd.is_production("testdata/y.go"))
        self.assertTrue(cd.is_production("internal/x/y.go"))


class MeasureTest(unittest.TestCase):
    def test_only_executable_changed_lines_count(self):
        p = cd.parse_profile(PROFILE, MOD)
        r = cd.measure({"internal/a/a.go": {10, 11, 16, 20}, "internal/c/c.go": {1}}, p)
        self.assertEqual(len(r), 1)
        self.assertEqual((r[0].covered, r[0].total, r[0].uncovered), (2, 3, [20]))
        self.assertIn("66.7 %", cd.render_diff(r, "origin/main", 80))

    def test_nothing_changed(self):
        self.assertIn("нет изменённых", cd.render_diff([], "origin/main", None))

    def test_ranges(self):
        self.assertEqual(cd.ranges([3, 4, 5, 9, 11, 12]), "3–5, 9, 11–12")
        self.assertEqual(cd.ranges([]), "")


if __name__ == "__main__":
    unittest.main()
