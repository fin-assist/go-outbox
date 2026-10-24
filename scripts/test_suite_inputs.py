"""Execution-evidence guard tests; no coverage calculation is implemented here."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


GUARD = Path(__file__).with_name("check-suite.sh").resolve()


class SuiteInputsTests(unittest.TestCase):
    def check(self, events, profile, expected):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "suite.jsonl"
            log.write_text("".join(json.dumps(event) + "\n" for event in events))
            coverage = root / "suite.out"
            if profile is not None:
                coverage.write_text(profile)
            result = subprocess.run(
                ["bash", str(GUARD), str(log), str(coverage)],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode == 0, expected, result.stderr)

    def test_executed_test_and_packages_without_tests(self):
        self.check([
            {"Action": "pass", "Test": "TestUnitRule"},
            {"Action": "skip", "Package": "example.com/service/domain"},
        ], "mode: atomic\nexample.com/service/rule.go:3.1,3.20 1 0\n", True)

    def test_package_pass_does_not_prove_test_execution(self):
        self.check([{"Action": "pass", "Package": "example.com/service"}],
                   "mode: atomic\nexample.com/service/rule.go:3.1,3.20 1 1\n", False)

    def test_empty_event_stream(self):
        self.check([], "mode: atomic\nexample.com/service/rule.go:3.1,3.20 1 1\n", False)

    def test_skipped_or_failed_test_is_not_coverage_evidence(self):
        for action in ["skip", "fail"]:
            with self.subTest(action=action):
                self.check([
                    {"Action": "pass", "Test": "TestUnitRule"},
                    {"Action": action, "Test": "TestUnitDatabase"},
                ], "mode: atomic\nexample.com/service/rule.go:3.1,3.20 1 1\n", False)

    def test_missing_empty_header_only_and_zero_statement_profiles(self):
        for profile in [None, "", "mode: atomic\n",
                        "mode: atomic\nexample.com/service/rule.go:3.1,3.20 0 1\n"]:
            with self.subTest(profile=profile):
                self.check([{"Action": "pass", "Test": "TestUnitRule"}], profile, False)

    def test_malformed_execution_log(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "suite.jsonl"
            log.write_text("not JSON\n")
            result = subprocess.run(["bash", str(GUARD), str(log), str(log)],
                                    capture_output=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
