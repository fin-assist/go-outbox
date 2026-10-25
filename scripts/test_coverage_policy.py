"""Check the pinned external tool's agreed rounding, without duplicating its math."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class CoveragePolicyTests(unittest.TestCase):
    def test_agreed_threshold_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # Weighted profile fixtures exercise the tool, not a local percentage calculator.
            (root / "go.mod").write_text("module example.com/policy\n\ngo 1.26.0\n")
            (root / "policy.go").write_text(
                "package policy\n\nfunc Covered() { _ = 1 }\nfunc Uncovered() { _ = 2 }\n")
            tool = os.environ.get("GO_TEST_COVERAGE_BIN")
            if tool:
                command = [tool]
            else:
                command = ["go", "run", "github.com/vladopajic/go-test-coverage/v2@v2.20.1"]
            for covered, missed, success in [(1600, 400, True), (1599, 401, True),
                                             (1598, 402, False), (0, 2000, False)]:
                with self.subTest(covered=covered, missed=missed):
                    (root / "coverage.out").write_text(
                        "mode: atomic\n"
                        f"example.com/policy/policy.go:3.16,3.25 {covered} 1\n"
                        f"example.com/policy/policy.go:4.18,4.27 {missed} 0\n")
                    result = subprocess.run(command + ["--profile", "coverage.out",
                                                        "--threshold-total", "80"],
                                            cwd=root, capture_output=True, text=True, timeout=180,
                                            env=dict(os.environ, GOTOOLCHAIN="auto"))
                    self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
