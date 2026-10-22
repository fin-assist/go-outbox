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


class ReportTest(unittest.TestCase):
    def test_totals_count_lines_once_and_exclude_nonproduction(self):
        p = cd.parse_profile(PROFILE + f"{MOD}/internal/a/a.go:50.1,60.1 0 1\n", MOD)
        p["a_test.go"] = {1: True}
        p["testdata/a.go"] = {1: True}
        self.assertEqual(cd.totals(p), (6, 8))
        self.assertIn("75.0 %", cd.render_totals(*cd.totals(p)))
        self.assertNotIn("100.0 %", cd.render_totals(0, 0))
        self.assertIn("Нет данных", cd.render_totals(0, 0))

    def test_generated_production_is_included(self):
        self.assertTrue(cd.is_production("gen/go/finerr/errors.gen.go"))
        self.assertTrue(cd.is_production("finassist/common/v1/common.pb.go"))

    def test_cli_combines_totals_and_diff_and_keeps_diff_threshold(self):
        import contextlib
        import io
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "go.mod").write_text(f"module {MOD}\n")
            profile = root / "cover.out"
            profile.write_text(PROFILE)
            summary = root / "summary.md"
            with patch.object(cd, "ROOT", root), patch.object(cd, "git_diff", return_value=DIFF):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    status = cd.main(["--profile", str(profile), "--base", "origin/main", "--min", "70", "--summary", str(summary)])
            self.assertEqual(status, 1)  # total 75 %, changed 66.7 %
            text = out.getvalue()
            self.assertLess(text.index("### Покрытие проекта"), text.index("### Покрытие изменённых строк"))
            self.assertIn("75.0 %", text)
            self.assertIn("66.7 %", text)
            self.assertIn("| `internal/a/a.go`", text)
            self.assertEqual(summary.read_text(), text + "\n")

    def test_multiple_modules_keep_repository_paths_and_merge_repeated_profiles(self):
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inputs = []
            for directory, covered in [("gen/go", 1), ("tools", 0)]:
                module = root / directory
                module.mkdir(parents=True)
                (module / "go.mod").write_text(f"module example.com/{directory}\n")
                profile = module / "cover.out"
                profile.write_text(f"mode: atomic\nexample.com/{directory}/same.go:1.1,2.2 1 {covered}\n")
                inputs.append((module, profile))
            with patch.object(cd, "ROOT", root):
                p = cd.load_profiles(inputs + [inputs[0]])
            self.assertEqual(set(p), {"gen/go/same.go", "tools/same.go"})
            self.assertEqual(cd.totals(p), (2, 4))
            self.assertEqual(cd.measure({"tools/same.go": {1}}, p)[0].uncovered, [1])

    def test_empty_scaffold_has_both_sections_without_percentage(self):
        import contextlib
        import io
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(cd, "ROOT", Path(tmp)), patch.object(cd, "git_diff", return_value=""):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(cd.main(["--empty", "--base", "origin/main"]), 0)
                self.assertIn("### Покрытие проекта", out.getvalue())
                self.assertIn("### Покрытие изменённых строк", out.getvalue())
                self.assertNotIn("100.0 %", out.getvalue())

    def test_empty_mode_rejects_existing_module(self):
        import contextlib
        import io
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "go.mod").write_text(f"module {MOD}\n")
            with patch.object(cd, "ROOT", root), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    cd.main(["--empty"])
            self.assertEqual(raised.exception.code, 2)

    def test_workflow_uses_single_coverage_test_run_and_keeps_fork_guard(self):
        workflow = Path(__file__).resolve().parent.parent / ".github/workflows/ci.yml"
        text = workflow.read_text()
        self.assertIn("github.event.pull_request.head.repo.full_name == github.repository", text)
        self.assertIn("<!-- coverage-report (ci.yml) -->", text)
        self.assertIn("--summary", text)
        # One execution per Go module, never a second run for project totals.
        commands = [line for line in text.splitlines() if "go test " in line and "-coverprofile=" in line]
        self.assertEqual(len(commands), 2 if "--module-profile" in text else 1)


if __name__ == "__main__":
    unittest.main()
