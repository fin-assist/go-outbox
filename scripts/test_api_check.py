"""Exercise the CI entry point with real local Git remotes and a deterministic Go boundary."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
VERSION = "v0.1.1"
EXISTS = (f"# summary\n{VERSION} is not a valid semantic version for this release.\n"
          f"version {VERSION} already exists\n\nexit status 1\n")
VALID = f"# summary\n{VERSION} is a valid semantic version for this release.\n"


class APICheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.owner = self.root / "owner"
        self.owner.mkdir()
        self.git(self.owner, "init", "-b", "main")
        self.git(self.owner, "config", "user.name", "API Test")
        self.git(self.owner, "config", "user.email", "api@example.invalid")
        self.git(self.owner, "config", "commit.gpgsign", "false")
        (self.owner / "CHANGELOG.md").write_text(
            "# Changelog\n\n## [Unreleased]\n\n## [v0.1.0] — 2026-10-24\n\n- First.\n")
        self.git(self.owner, "add", ".")
        self.git(self.owner, "commit", "-m", "First release")
        self.git(self.owner, "tag", "v0.1.0")
        self.previous = self.git(self.owner, "rev-parse", "HEAD")
        (self.owner / "CHANGELOG.md").write_text(
            "# Changelog\n\n## [Unreleased]\n\n"
            "## [v0.1.1] — 2026-10-25\n\n- Fix.\n\n"
            "## [v0.1.0] — 2026-10-24\n\n- First.\n")
        self.git(self.owner, "add", ".")
        self.git(self.owner, "commit", "-m", "Prepare release")
        self.head = self.git(self.owner, "rev-parse", "HEAD")
        self.clone = self.root / "checkout"
        self.git(self.root, "clone", str(self.owner), str(self.clone))
        # Execute the actual YAML step. Fake only go run, using a shell function
        # to avoid executable fixture/shebang restrictions on the agent host.
        (self.clone / "scripts").mkdir()
        for name in ("api_check.py", "changelog.py"):
            source = ROOT / "scripts" / name
            if source.exists():
                (self.clone / "scripts" / name).write_bytes(source.read_bytes())
        self.fake = self.root / "fake_go.py"
        self.fake.write_text('''
import json, os, subprocess, sys
from pathlib import Path
root = Path(os.environ["FIXTURE_ROOT"])
with (root / "calls.jsonl").open("a") as out:
    out.write(json.dumps(sys.argv[1:]) + "\\n")
state = json.loads((root / "state.json").read_text())
if state.get("publish"):
    command = ["git", "-C", str(root / "owner"), "tag"]
    if state.get("annotated"):
        command += ["-a", "-m", "Release"]
    if state.get("replace"):
        command += ["-f"]
    command += ["v0.1.1", state["publish"]]
    subprocess.run(command, check=True, capture_output=True, timeout=10)
if state.get("remote_error"):
    subprocess.run(["git", "remote", "set-url", "origin", str(root / "missing")],
                   check=True, capture_output=True, timeout=10)
print(state["report"], end="")
sys.exit(state.get("status", 0))
''')

    @staticmethod
    def git(directory, *args):
        return subprocess.run(["git", *args], cwd=directory, check=True,
                              capture_output=True, text=True, timeout=10).stdout.strip()

    def publish(self, commit=None, annotated=False, fetch=False):
        args = ["tag"]
        if annotated:
            args += ["-a", "-m", "Release"]
        self.git(self.owner, *args, VERSION, commit or self.head)
        if fetch:
            self.git(self.clone, "fetch", "--tags", "origin")

    def run_check(self, report=VALID, status=0, **state):
        (self.root / "state.json").write_text(json.dumps(dict(report=report, status=status, **state)))
        workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
        script = next(step["run"] for step in workflow["jobs"]["api"]["steps"]
                      if step.get("name") == "API changes")
        env = dict(os.environ, FIXTURE_ROOT=str(self.root), FAKE_PYTHON=sys.executable,
                   FAKE_GO=str(self.fake), GITHUB_STEP_SUMMARY=str(self.root / "summary.md"),
                   GORELEASE=workflow["env"]["GORELEASE"])
        # Exported function also reaches the Python runner's bash subprocess.
        prefix = 'go() { "$FAKE_PYTHON" "$FAKE_GO" "$@"; }; export -f go\n'
        result = subprocess.run(["/bin/bash", "-e", "-o", "pipefail", "-c", prefix + script],
                                cwd=self.clone, env=env, capture_output=True, text=True, timeout=20)
        log = self.root / "calls.jsonl"
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return result, calls

    def assert_passes(self, **kwargs):
        result, calls = self.run_check(**kwargs)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return calls

    def assert_fails(self, **kwargs):
        result, calls = self.run_check(**kwargs)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return calls

    def test_pending_candidate_validates_against_previous_release(self):
        calls = self.assert_passes()
        self.assertIn("-base=v0.1.0", calls[0])
        self.assertIn("-version=" + VERSION, calls[0])

    def test_publication_during_gorelease_is_accepted_only_for_same_commit(self):
        for annotated in (False, True):
            with self.subTest(annotated=annotated):
                if annotated:
                    self.git(self.owner, "tag", "-d", VERSION)
                self.assert_passes(report=EXISTS, status=1, publish=self.head, annotated=annotated)

    def test_publication_between_checkout_and_check_is_accepted(self):
        self.publish(annotated=True)
        self.assert_passes(report=EXISTS, status=1)

    def test_already_published_same_commit_still_compares_previous_version(self):
        self.publish(fetch=True)
        calls = self.assert_passes(report=EXISTS, status=1)
        self.assertIn("-base=v0.1.0", calls[0])
        self.assertIn("-version=" + VERSION, calls[0])

    def test_foreign_commit_before_or_during_check_fails(self):
        calls = self.assert_fails(report=EXISTS, status=1, publish=self.previous)
        self.assertEqual(len(calls), 1)
        # A second check sees the foreign tag and never invokes go again.
        self.assertEqual(self.assert_fails(report=EXISTS, status=1), calls)

    def test_successful_report_does_not_hide_concurrent_foreign_tag(self):
        self.assert_fails(publish=self.previous)

    def test_already_exists_without_remote_tag_is_not_success(self):
        self.assert_fails(report=EXISTS, status=1)

    def test_other_version_or_tool_errors_are_not_suppressed(self):
        self.publish(fetch=True)
        for report in ("network failed\n", EXISTS + "unexpected error\n",
                       EXISTS.replace("already exists", "has an invalid module path"),
                       EXISTS.replace("already exists", "has incompatible changes"),
                       EXISTS.replace(VERSION, "v0.1.2"),
                       "## errors in base version:\nmissing package\n\n" + EXISTS,
                       "## errors in release version:\ncompile error\n\n" + EXISTS,
                       VALID + "unexpected error\n"):
            with self.subTest(report=report):
                self.assert_fails(report=report, status=1)

    def test_valid_version_with_dependency_diagnostic_keeps_existing_policy(self):
        self.assert_passes(report="# diagnostics\nretracted dependency\n\n" + VALID + "exit status 1\n",
                           status=1)

    def test_remote_failure_after_report_fails_closed(self):
        self.assert_fails(remote_error=True)

    def test_remote_failure_before_report_never_runs_gorelease(self):
        self.git(self.clone, "remote", "set-url", "origin", str(self.root / "missing"))
        self.assertEqual(self.assert_fails(), [])

    def test_first_release_uses_no_base(self):
        self.git(self.owner, "tag", "-d", "v0.1.0")
        self.git(self.clone, "tag", "-d", "v0.1.0")
        calls = self.assert_passes()
        self.assertIn("-base=none", calls[0])

    def test_ordinary_changes_after_release_remain_informational(self):
        self.publish(fetch=True)
        (self.clone / "development.txt").write_text("Next development change\n")
        self.git(self.clone, "config", "user.name", "API Test")
        self.git(self.clone, "config", "user.email", "api@example.invalid")
        self.git(self.clone, "config", "commit.gpgsign", "false")
        self.git(self.clone, "add", "development.txt")
        self.git(self.clone, "commit", "-m", "Continue development")
        calls = self.assert_passes(report="# summary\nSuggested version: v0.1.2\n")
        self.assertIn("-base=" + VERSION, calls[0])
        self.assertFalse(any(arg.startswith("-version=") for arg in calls[0]))

    def test_preexisting_tag_cannot_move_during_check(self):
        self.publish(fetch=True)
        self.assert_fails(report=EXISTS, status=1, publish=self.previous, replace=True)

    def test_candidate_version_errors_fail_before_publication_too(self):
        self.assert_fails(report=EXISTS.replace("already exists", "has incompatible changes"), status=1)

    def test_abnormal_tool_exit_cannot_reuse_a_valid_summary(self):
        self.assert_fails(report=VALID, status=2)

    def test_no_release_section_keeps_initial_report_informational(self):
        (self.clone / "CHANGELOG.md").write_text("# Changelog\n\n## [Unreleased]\n")
        self.git(self.clone, "tag", "-d", "v0.1.0")
        calls = self.assert_passes(report="# summary\nSuggested version: v0.1.0\n")
        self.assertIn("-base=none", calls[0])
        self.assertFalse(any(arg.startswith("-version=") for arg in calls[0]))

    def test_known_tag_outside_checkout_history_fails(self):
        self.git(self.owner, "checkout", "-b", "side", self.previous)
        (self.owner / "side.txt").write_text("Unmerged branch\n")
        self.git(self.owner, "add", "side.txt")
        self.git(self.owner, "commit", "-m", "Unmerged release")
        self.publish(commit=self.git(self.owner, "rev-parse", "HEAD"), fetch=True)
        self.assertEqual(self.assert_fails(), [])


    def test_local_tag_diverging_from_remote_fails(self):
        self.publish(fetch=True)
        self.git(self.owner, "tag", "-f", VERSION, self.previous)
        self.assertEqual(self.assert_fails(), [])


if __name__ == "__main__":
    unittest.main()
