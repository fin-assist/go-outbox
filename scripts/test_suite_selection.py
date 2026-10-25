"""The race-enabled collectors must reject tests outside the three suites."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import unittest


MAKEFILE = Path(__file__).resolve().parents[1] / "Makefile"


class SuiteSelectionTests(unittest.TestCase):
    def run_make(self, root, target):
        process = subprocess.Popen(
            ["make", target], cwd=root, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, start_new_session=True,
        )
        try:
            output, _ = process.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            self.fail("suite classification exceeded 60 seconds")
        return process.returncode, output

    def test_race_only_unclassified_test_blocks_collection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copyfile(MAKEFILE, root / "Makefile")
            (root / "go.mod").write_text("module example.com/selection\n\ngo 1.26.0\n")
            (root / "rule_test.go").write_text(
                'package selection\nimport "testing"\n'
                'func TestUnitRule(t *testing.T) {}\n'
                'func TestIntegrationRule(t *testing.T) {}\n'
                'func TestAPIRule(t *testing.T) {}\n'
            )
            status, output = self.run_make(root, "test-names")
            self.assertEqual(status, 0, output)
            (root / "race_test.go").write_text(
                '//go:build race\n\npackage selection\nimport "testing"\n'
                'func TestRaceRegression(t *testing.T) { t.Fatal("must not be omitted") }\n'
            )
            for target in ["test-names", "collect-unit", "cover"]:
                with self.subTest(target=target):
                    status, output = self.run_make(root, target)
                    self.assertNotEqual(status, 0, output)
                    self.assertIn("TestRaceRegression", output)
                    self.assertIn("Name every test", output)
                    self.assertFalse(list(root.glob("*.jsonl")))
                    self.assertFalse(list(root.glob("*.out")))

    def test_executable_examples_and_fuzz_seeds_run_in_api_suite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copyfile(MAKEFILE, root / "Makefile")
            (root / "scripts").mkdir()
            shutil.copyfile(Path(__file__).with_name("check-suite.sh"), root / "scripts/check-suite.sh")
            (root / "go.mod").write_text("module example.com/selection\n\ngo 1.26.0\n")
            (root / "rule.go").write_text("package selection\nfunc Value() int { return 42 }\n")
            (root / "rule_test.go").write_text(
                'package selection\nimport ("fmt"; "testing")\n'
                'func TestAPIRule(t *testing.T) { if Value()!=42 { t.Fatal("value") } }\n'
                'func ExampleValue() { fmt.Println(Value())\n// Output: 42\n}\n'
                'func FuzzAPIValue(f *testing.F) { f.Add(42); f.Fuzz(func(t *testing.T, n int) {'
                ' if Value()!=42 { t.Fatal("value") } }) }\n'
            )
            status, output = self.run_make(root, "collect-api")
            self.assertEqual(status, 0, output)
            events = [json.loads(line) for line in (root / "api.jsonl").read_text().splitlines()]
            passed = {event.get("Test") for event in events if event["Action"] == "pass"}
            self.assertTrue({"TestAPIRule", "ExampleValue", "FuzzAPIValue"}.issubset(passed), passed)
            with (root / "rule_test.go").open("a") as file:
                file.write('func FuzzUnclassified(f *testing.F) { f.Add(1); f.Fuzz(func(t *testing.T, n int) {}) }\n')
            status, output = self.run_make(root, "test-names")
            self.assertNotEqual(status, 0, output)
            self.assertIn("FuzzUnclassified", output)

    def test_only_exact_pgtest_package_is_excluded_from_every_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copyfile(MAKEFILE, root / "Makefile")
            (root / "scripts").mkdir()
            shutil.copyfile(Path(__file__).with_name("check-suite.sh"), root / "scripts/check-suite.sh")
            (root / "go.mod").write_text("module example.com/scope\n\ngo 1.26.0\n")
            packages = {"": "scope", "internal/pgtest": "pgtest",
                        "internal/pgtest/nested": "nested", "internal/pgtestextra": "pgtestextra",
                        "raceonly": "raceonly"}
            for path, package in packages.items():
                folder = root / path
                folder.mkdir(parents=True, exist_ok=True)
                build = "//go:build race\n\n" if path == "raceonly" else ""
                (folder / "value.go").write_text(build + f"package {package}\nfunc Value() int {{ return 42 }}\n")
                (folder / "value_test.go").write_text(
                    build + f'package {package}\nimport "testing"\n' +
                    "".join(f'func Test{level}Value(t *testing.T) {{ if Value()!=42 {{ t.Fatal("value") }} }}\n'
                            for level in ["Unit", "Integration", "API"]))
            for level in ["unit", "integration", "api"]:
                with self.subTest(level=level):
                    status, output = self.run_make(root, "collect-" + level)
                    self.assertEqual(status, 0, output)
                    profile = (root / (level + ".out")).read_text()
                    self.assertNotIn("example.com/scope/internal/pgtest/value.go:", profile)
                    for path in ["value.go:", "internal/pgtest/nested/value.go:",
                                 "internal/pgtestextra/value.go:", "raceonly/value.go:"]:
                        self.assertIn("example.com/scope/" + path, profile)
                    events = [json.loads(line) for line in (root / (level + ".jsonl")).read_text().splitlines()]
                    self.assertTrue(any(event["Action"] == "pass" and
                                        event.get("Package") == "example.com/scope/internal/pgtest" and
                                        event.get("Test") == "Test" + {"unit": "Unit", "integration": "Integration", "api": "API"}[level] + "Value"
                                        for event in events), "excluded harness tests did not execute")

    def test_empty_production_package_list_fails_before_collection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copyfile(MAKEFILE, root / "Makefile")
            (root / "go.mod").write_text("module example.com/empty\n\ngo 1.26.0\n")
            folder = root / "internal/pgtest"
            folder.mkdir(parents=True)
            (folder / "helper.go").write_text("package pgtest\nfunc Value() int { return 42 }\n")
            (folder / "helper_test.go").write_text('package pgtest\nimport "testing"\nfunc TestUnitValue(t *testing.T) {}\n')
            for level in ["unit", "integration", "api"]:
                with self.subTest(level=level):
                    status, output = self.run_make(root, "collect-" + level)
                    self.assertNotEqual(status, 0, output)
                    self.assertIn("No production packages remain for coverage.", output)
                    self.assertFalse((root / (level + ".out")).exists())
                    self.assertFalse((root / (level + ".jsonl")).exists())


if __name__ == "__main__":
    unittest.main()
