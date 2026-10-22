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

    def test_missing_coverage_never_claims_no_changed_code_or_passes_threshold(self):
        import contextlib
        import io
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "go.mod").write_text(f'module "{MOD}"\n')
            profile = root / "cover.out"
            profile.write_text("mode: atomic\n")
            with patch.object(cd, "ROOT", root), patch.object(cd, "git_diff", return_value=DIFF):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    status = cd.main(["--profile", str(profile), "--base", "origin/main", "--min", "80"])
                self.assertEqual(status, 1)
                self.assertIn("Нет данных покрытия", out.getvalue())
                self.assertNotIn("нет изменённых исполняемых строк", out.getvalue())
                self.assertEqual(cd.module_path(root), MOD)

    def test_low_project_coverage_does_not_fail_high_changed_coverage(self):
        import contextlib
        import io
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "go.mod").write_text(f"module {MOD}\n")
            profile = root / "cover.out"
            profile.write_text(PROFILE)
            changed = "+++ b/internal/a/a.go\n@@ -10 +10 @@\n"
            with patch.object(cd, "ROOT", root), patch.object(cd, "git_diff", return_value=changed):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(cd.main(["--profile", str(profile), "--base", "origin/main", "--min", "80"]), 0)
                self.assertIn("75.0 %", out.getvalue())
                self.assertIn("100.0 %", out.getvalue())

    def test_empty_rejects_nested_modules_and_input_modes_are_exclusive(self):
        import contextlib
        import io
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tools").mkdir()
            (root / "tools/go.mod").write_text("module example.com/tools\n")
            for args in [["--empty"], ["--profile", "a", "--module-profile", "tools", "b"], ["--empty", "--profile", "a"]]:
                with patch.object(cd, "ROOT", root), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        cd.main(args)
                    self.assertEqual(raised.exception.code, 2)

    def test_cli_relative_module_profiles_and_invalid_directory(self):
        import contextlib
        import io
        import os
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, count in [("gen/go", 1), ("tools", 0)]:
                directory = root / name
                directory.mkdir(parents=True)
                (directory / "go.mod").write_text(f"module example.com/{name}\n")
                (directory / "cover.out").write_text(f"mode: atomic\nexample.com/{name}/same.go:1.1,2.2 1 {count}\n")
            previous = Path.cwd()
            try:
                os.chdir(root / "gen")
                with patch.object(cd, "ROOT", root), contextlib.redirect_stdout(io.StringIO()) as out:
                    status = cd.main(["--module-profile", "go", "go/cover.out", "--module-profile", "../tools", "../tools/cover.out"])
                    self.assertEqual(status, 0)
                    self.assertIn("50.0 %", out.getvalue())
                    self.assertIn("2 из 4", out.getvalue())
                (root / "go.mod").write_text(f"module {MOD}\n")
                (root / "cover.out").write_text(PROFILE)
                with patch.object(cd, "ROOT", root), contextlib.redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(cd.main(["--profile", "../cover.out", "--summary", "summary.md"]), 0)
                    self.assertIn("75.0 %", out.getvalue())
                    self.assertEqual((root / "gen/summary.md").read_text(), out.getvalue() + "\n")
                for args in [["--empty", "--module-profile", "go", "go/cover.out"], ["--module-profile", "../..", "missing.out"]]:
                    with patch.object(cd, "ROOT", root), contextlib.redirect_stderr(io.StringIO()):
                        with self.assertRaises(SystemExit) as raised:
                            cd.main(args)
                        self.assertEqual(raised.exception.code, 2)
            finally:
                os.chdir(previous)

    def test_failed_or_empty_report_keeps_last_comment(self):
        import os
        import re
        import subprocess
        import tempfile
        workflow = Path(__file__).resolve().parent.parent / ".github/workflows/ci.yml"
        text = workflow.read_text()
        self.assertIn("steps.coverage.outcome == 'success'", text)
        section = text.split("      - name: Coverage comment on the PR\n", 1)[1]
        self.assertIn("        shell: bash", section)
        # Read the complete publishing step, including every shell line indented by ten spaces.
        lines = section.split("        run: |\n", 1)[1].splitlines()
        script_lines = []
        for line in lines:
            if line and not line.startswith("          "):
                break
            script_lines.append(line[10:] if line else "")
        shell = re.sub(r"\$\{\{ github.repository \}\}", "fin-assist/example", "\n".join(script_lines))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gh = root / "gh"
            gh.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$GH_CALLS"\nif [ "${GH_GET_FAIL:-0}" = 1 ]; then exit 1; fi\nprintf "%s\\n" "${GH_COMMENTS:-[]}"\n')
            gh.chmod(0o755)
            calls = root / "calls"
            env = dict(os.environ, PATH=f"{root}:{os.environ['PATH']}", GH_CALLS=str(calls), PR="7", HEAD_SHA="abcdef123", RUN_URL="https://example.test/run")
            for content in [None, "", "### Покрытие проекта\n\nНет данных\n"]:
                profile = root / "coverage.md"
                if content is not None:
                    profile.write_text(content)
                result = subprocess.run(["bash", "-eo", "pipefail", "-c", shell], cwd=root, env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                if not content:
                    self.assertFalse(calls.exists(), "an absent/empty report must never replace a valid comment")
                else:
                    self.assertEqual(calls.read_text().splitlines(), [
                        "api repos/fin-assist/example/issues/7/comments --paginate",
                        "api repos/fin-assist/example/issues/7/comments -F body=@comment.md",
                    ])
                    comment = (root / "comment.md").read_text()
                    self.assertTrue(comment.startswith("<!-- coverage-report (ci.yml) -->"))
                    self.assertIn("### Покрытие проекта", comment)
                    self.assertIn("Commit abcdef1", comment)
            import json
            calls.unlink()
            env["GH_COMMENTS"] = json.dumps([{"id": 123, "user": {"login": "github-actions[bot]"}, "body": "<!-- coverage-report (ci.yml) --> old valid report"}])
            result = subprocess.run(["bash", "-eo", "pipefail", "-c", shell], cwd=root, env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(calls.read_text().splitlines(), [
                "api repos/fin-assist/example/issues/7/comments --paginate",
                "api -X PATCH repos/fin-assist/example/issues/comments/123 -F body=@comment.md",
            ])
            calls.unlink()
            env["GH_GET_FAIL"] = "1"
            result = subprocess.run(["bash", "-eo", "pipefail", "-c", shell], cwd=root, env=env, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(calls.read_text().splitlines(), [
                "api repos/fin-assist/example/issues/7/comments --paginate",
            ], "a failed lookup must not POST or PATCH any comment")

    def test_workflow_uses_single_coverage_test_run_and_keeps_fork_guard(self):
        workflow = Path(__file__).resolve().parent.parent / ".github/workflows/ci.yml"
        text = workflow.read_text()
        step = text.split("      - name: Coverage comment on the PR\n", 1)[1].split("\n      - name:", 1)[0]
        condition = next(line for line in step.splitlines() if line.strip().startswith("if:"))
        self.assertIn("github.event.pull_request.head.repo.full_name == github.repository", condition)
        self.assertIn("steps.coverage.outcome == 'success'", condition)
        self.assertIn("<!-- coverage-report (ci.yml) -->", text)
        self.assertIn("--summary", text)
        # One execution per Go module, never a second run for project totals.
        commands = [line for line in text.splitlines() if "go test " in line and "-coverprofile=" in line]
        repository = workflow.parent.parent.parent
        modules = {p.parent.relative_to(repository).as_posix() for p in repository.rglob("go.mod")}
        self.assertEqual(len(commands), max(1, len(modules)))
        executed_modules = []
        current_directory = "."
        for line in text.splitlines():
            if line.startswith("      - "):
                current_directory = "."
            if line.startswith("        working-directory:"):
                current_directory = line.split(":", 1)[1].strip()
            if "go test " in line and "-coverprofile=" in line:
                executed_modules.append(current_directory)
        self.assertEqual(sorted(executed_modules), sorted(modules or {"."}))
        profile_line = next((line for line in text.splitlines() if "--module-profile" in line and "--base" in line), "")
        if profile_line:
            import re
            reported_modules = set(re.findall(r"--module-profile\s+(\S+)\s+\S+", profile_line))
            self.assertEqual(reported_modules, modules)


if __name__ == "__main__":
    unittest.main()
