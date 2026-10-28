"""Exercise the CI entry point with real local Git remotes and a deterministic Go boundary."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import api_check
import yaml


ROOT = Path(__file__).resolve().parents[1]
COMMON = Path(subprocess.check_output(
    [sys.executable, str(ROOT / "scripts/common_ci.py")], text=True, timeout=180).strip())
SHARED_API_WORKFLOW = COMMON.parent / ".github/workflows/shared-api.yml"
VERSION = "v0.1.1"
# Complete stdout + stderr captured from the pinned go run on a file:// proxy.
RECORDED_GORELEASE = 'golang.org/x/exp/cmd/gorelease@v0.0.0-20261014143641-f0251eb4aa3e'
EXISTS = '# summary\nv0.1.1 is not a valid semantic version for this release.\nversion v0.1.1 already exists\n\nexit status 1\n'
VALID = '# summary\nv0.1.1 is a valid semantic version for this release.\n'
EXISTS_INCOMPATIBLE_V1 = '# example.invalid/fin264-report-fixture\n## incompatible changes\nGreeting: removed\n\n# summary\nv1.0.1 is not a valid semantic version for this release.\nThere are incompatible changes.\n\nexit status 1\n'
EXISTS_COMPATIBLE_PATCH_V1 = '# example.invalid/fin264-report-fixture\n## compatible changes\nExtra: added\n\n# summary\nv1.0.1 is not a valid semantic version for this release.\nThere are compatible changes, but the minor version is not incremented\nover the base version (v1.0.0).\n\nexit status 1\n'



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
if state.get("delete"):
    subprocess.run(["git", "-C", str(root / "owner"), "tag", "-d", "v0.1.1"],
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
        workflow = yaml.safe_load(SHARED_API_WORKFLOW.read_text())
        script = next(step["run"] for step in workflow["jobs"]["check"]["steps"]
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

    def assert_fails(self, reason=None, **kwargs):
        result, calls = self.run_check(**kwargs)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("API check failed:", result.stderr)
        self.assertNotIn("Traceback", result.stdout + result.stderr)
        if reason:
            self.assertIn(reason, result.stderr)
        return calls

    def test_captured_reports_require_the_recorded_tool_pin(self):
        workflow = yaml.safe_load(SHARED_API_WORKFLOW.read_text())
        self.assertEqual(workflow["env"]["GORELEASE"], RECORDED_GORELEASE)

    def test_pending_candidate_validates_against_previous_release(self):
        calls = self.assert_passes()
        self.assertIn("-base=v0.1.0", calls[0])
        self.assertIn("-version=" + VERSION, calls[0])

    def test_publication_during_gorelease_is_accepted_only_for_same_commit(self):
        for report, status in ((VALID, 0), (EXISTS, 1)):
            for annotated in (False, True):
                with self.subTest(annotated=annotated, status=status):
                    if self.git(self.owner, "tag", "--list", VERSION):
                        self.git(self.owner, "tag", "-d", VERSION)
                    self.assert_passes(report=report, status=status, publish=self.head, annotated=annotated)

    def test_publication_between_checkout_and_check_is_accepted(self):
        self.publish(annotated=True)
        self.assert_passes(report=EXISTS, status=1)

    def test_already_published_same_commit_still_compares_previous_version(self):
        for annotated in (False, True):
            with self.subTest(annotated=annotated):
                if annotated:
                    self.git(self.owner, "tag", "-d", VERSION)
                    self.git(self.clone, "tag", "-d", VERSION)
                self.publish(annotated=annotated, fetch=True)
                calls = self.assert_passes(report=EXISTS, status=1)
                self.assertIn("-base=v0.1.0", calls[-1])
                self.assertIn("-version=" + VERSION, calls[-1])

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

    def test_published_incompatible_v1_patch_is_still_rejected(self):
        text = (self.owner / "CHANGELOG.md").read_text().replace(VERSION, "v1.0.1")
        (self.owner / "CHANGELOG.md").write_text(text)
        self.git(self.owner, "add", "CHANGELOG.md")
        self.git(self.owner, "commit", "-m", "Prepare v1 patch")
        head = self.git(self.owner, "rev-parse", "HEAD")
        self.git(self.owner, "tag", "v1.0.0", self.previous)
        self.git(self.owner, "tag", "-a", "-m", "Published v1 patch", "v1.0.1", head)
        self.git(self.clone, "fetch", "--tags", "origin")
        self.git(self.clone, "checkout", "--detach", head)
        for report in (EXISTS_INCOMPATIBLE_V1, EXISTS_COMPATIBLE_PATCH_V1):
            with self.subTest(report=report):
                calls = self.assert_fails(report=report, status=1)
                self.assertIn("-base=v1.0.0", calls[-1])
                self.assertIn("-version=v1.0.1", calls[-1])

    def test_release_parent_can_predate_changelog(self):
        self.git(self.owner, "rm", "CHANGELOG.md")
        self.git(self.owner, "commit", "-m", "Parent without changelog")
        (self.owner / "CHANGELOG.md").write_text(
            (self.clone / "CHANGELOG.md").read_text().replace(VERSION, "v0.1.2"))
        self.git(self.owner, "add", "CHANGELOG.md")
        self.git(self.owner, "commit", "-m", "Introduce changelog release")
        self.git(self.clone, "fetch", "origin")
        self.git(self.clone, "checkout", "--detach", self.git(self.owner, "rev-parse", "HEAD"))
        calls = self.assert_passes(report=VALID.replace(VERSION, "v0.1.2"))
        self.assertIn("-version=v0.1.2", calls[0])

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
        self.assert_fails(report=VALID + "exit status 2\n", status=1)

    def test_remote_tag_deletion_during_check_fails(self):
        self.publish(fetch=True)
        self.assert_fails(report=EXISTS, status=1, delete=True)

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

    def test_wrong_ancestor_tag_already_in_checkout_still_fails(self):
        self.publish(commit=self.previous, fetch=True)
        self.assertEqual(self.assert_fails(report=EXISTS, status=1), [])

    def test_release_merge_rejects_tag_on_its_preparation_commit(self):
        self.git(self.owner, "checkout", "-B", "main", self.previous)
        self.git(self.owner, "merge", "--no-ff", "-m", "Merge release PR", self.head)
        merge = self.git(self.owner, "rev-parse", "HEAD")
        self.publish(commit=self.head, fetch=True)
        self.git(self.clone, "checkout", "--detach", merge)
        self.assertEqual(self.assert_fails(report=EXISTS, status=1), [])

    def test_ordinary_development_cannot_reuse_an_unrelated_historical_tag(self):
        self.publish(commit=self.previous, fetch=True)
        (self.clone / "CHANGELOG.md").write_text((self.clone / "CHANGELOG.md").read_text() + "\n")
        self.git(self.clone, "config", "user.name", "API Test")
        self.git(self.clone, "config", "user.email", "api@example.invalid")
        self.git(self.clone, "config", "commit.gpgsign", "false")
        self.git(self.clone, "add", "CHANGELOG.md")
        self.git(self.clone, "commit", "-m", "Continue after release preparation")
        self.assertEqual(self.assert_fails(), [])

    def test_published_release_rerun_ignores_later_releases_for_base(self):
        self.publish(fetch=True)
        (self.owner / "future.txt").write_text("A later release\n")
        self.git(self.owner, "add", "future.txt")
        self.git(self.owner, "commit", "-m", "Later release")
        self.git(self.owner, "tag", "v0.1.2")
        self.git(self.clone, "fetch", "--tags", "origin")
        calls = self.assert_passes(report=EXISTS, status=1)
        self.assertIn("-base=v0.1.0", calls[0])

    def advance_checkout(self):
        (self.clone / "development.txt").write_text("Development after release\n")
        self.git(self.clone, "config", "user.name", "API Test")
        self.git(self.clone, "config", "user.email", "api@example.invalid")
        self.git(self.clone, "config", "commit.gpgsign", "false")
        self.git(self.clone, "add", "development.txt")
        self.git(self.clone, "commit", "-m", "Continue development")

    def test_development_checkout_before_publication_reads_remote_release(self):
        self.advance_checkout()
        self.publish()
        calls = self.assert_passes(report="# summary\nSuggested version: v0.1.2\n")
        self.assertIn("-base=" + VERSION, calls[0])
        self.assertFalse(any(arg.startswith("-version=") for arg in calls[0]))

    def test_development_with_publication_during_check_validates_pending_version(self):
        self.advance_checkout()
        calls = self.assert_passes(report=EXISTS,
                                  status=1, publish=self.head)
        self.assertIn("-base=v0.1.0", calls[0])
        self.assertIn("-version=" + VERSION, calls[0])

    def test_pending_development_cannot_hide_version_errors_during_publication(self):
        self.advance_checkout()
        self.assert_fails(report=EXISTS.replace("already exists", "has incompatible changes"),
                          status=1, publish=self.head, reason="did not accept")

    def test_legacy_releases_pin_exact_module_version_and_ancestor_sha(self):
        releases = {
            "go-identity": "f44f31941c19197cf92c007d4fe087b1d76557b0",
            "go-outbox": "d5043a4e5791338eb1d3c3ae0ba07d4bfb95a402",
            "go-tombstone": "0d6edd6ca9419c99bcb490cdb732c962ba6887d6",
        }
        for name, sha in releases.items():
            with self.subTest(module=name), mock.patch.object(api_check, "Path") as path, \
                    mock.patch.object(api_check, "git") as git:
                path.return_value.read_text.return_value = f"module github.com/fin-assist/{name}\n"
                self.assertEqual(api_check.release_commit("v0.1.0"), sha)
                git.assert_called_once_with("merge-base", "--is-ancestor", sha, "HEAD")

    def test_legacy_module_name_does_not_accept_a_different_history(self):
        (self.clone / "go.mod").write_text("module github.com/fin-assist/go-identity\n")
        (self.clone / "CHANGELOG.md").write_text("# Changelog\n\n## [Unreleased]\n\n"
                                                "## [v0.1.0] — 2026-10-24\n\n- First.\n")
        self.assertEqual(self.assert_fails(), [])

    def test_legacy_exception_does_not_cover_new_versions(self):
        (self.clone / "go.mod").write_text("module github.com/fin-assist/go-identity\n")
        calls = self.assert_passes()
        self.assertIn("-version=" + VERSION, calls[0])

    def test_later_main_tag_is_checked_and_cannot_validate_original_candidate(self):
        self.advance_checkout()
        later = self.git(self.clone, "rev-parse", "HEAD")
        self.git(self.owner, "fetch", str(self.clone), later)
        self.publish(commit=later, fetch=True)
        calls = self.assert_passes(report=EXISTS, status=1)
        self.assertIn("-base=v0.1.0", calls[0])
        self.assertIn("-version=" + VERSION, calls[0])
        # The exact release-candidate checkout must still reject this other commit.
        self.git(self.clone, "checkout", "--detach", self.head)
        self.assertEqual(self.assert_fails(report=EXISTS, status=1), calls)

    def test_later_main_tag_published_during_development_is_accepted(self):
        self.advance_checkout()
        later = self.git(self.clone, "rev-parse", "HEAD")
        self.git(self.owner, "fetch", str(self.clone), later)
        calls = self.assert_passes(report=EXISTS,
                                  status=1, publish=later)
        self.assertIn("-version=" + VERSION, calls[0])

    def test_published_late_head_does_not_hide_version_errors(self):
        self.advance_checkout()
        later = self.git(self.clone, "rev-parse", "HEAD")
        self.git(self.owner, "fetch", str(self.clone), later)
        self.publish(commit=later, fetch=True)
        calls = self.assert_fails(report=EXISTS.replace("already exists", "has incompatible changes"), status=1)
        self.assertIn("-version=" + VERSION, calls[0])

    def reintroduce_release(self, merge=False):
        old_release = self.head
        release_text = (self.owner / "CHANGELOG.md").read_text()
        (self.owner / "CHANGELOG.md").write_text(self.git(self.owner, "show", self.previous + ":CHANGELOG.md") + "\n")
        self.git(self.owner, "add", "CHANGELOG.md")
        self.git(self.owner, "commit", "-m", "Revert unpublished release")
        if merge:
            self.git(self.owner, "checkout", "-b", "retry-release")
        (self.owner / "CHANGELOG.md").write_text(release_text)
        self.git(self.owner, "add", "CHANGELOG.md")
        self.git(self.owner, "commit", "-m", "Reintroduce corrected release")
        if merge:
            self.git(self.owner, "checkout", "main")
            self.git(self.owner, "merge", "--no-ff", "-m", "Synthetic retry release PR merge", "retry-release")
        self.head = self.git(self.owner, "rev-parse", "HEAD")
        self.git(self.clone, "fetch", "origin")
        self.git(self.clone, "checkout", "--detach", self.head)
        return old_release

    def test_reintroduced_release_keeps_version_gate_and_accepts_its_publication(self):
        self.reintroduce_release()
        calls = self.assert_fails(report=EXISTS.replace("already exists", "has incompatible changes"), status=1)
        self.assertIn("-version=" + VERSION, calls[0])
        self.assertIn("-base=v0.1.0", calls[0])
        self.assert_passes(report=EXISTS, status=1, publish=self.head)

    def test_reintroduced_release_pr_merge_keeps_version_gate(self):
        self.reintroduce_release(merge=True)
        calls = self.assert_fails(report=EXISTS.replace("already exists", "has incompatible changes"), status=1)
        self.assertIn("-version=" + VERSION, calls[0])
        self.assertIn("-base=v0.1.0", calls[0])

    def test_reintroduced_release_rejects_publication_of_reverted_attempt(self):
        old = self.reintroduce_release()
        self.assert_fails(report=EXISTS, status=1, publish=old, reason="does not match release")

    def test_restoring_published_release_section_keeps_immutable_baseline(self):
        self.publish(fetch=True)
        self.reintroduce_release()
        calls = self.assert_passes(report="# summary\nSuggested version: v0.1.2\n")
        self.assertIn("-base=" + VERSION, calls[-1])
        self.assertFalse(any(arg.startswith("-version=") for arg in calls[-1]))
        self.advance_checkout()
        calls = self.assert_passes(report="# summary\nSuggested version: v0.1.2\n")
        self.assertFalse(any(arg.startswith("-version=") for arg in calls[-1]))

    def test_unpublished_typo_retry_rejects_tag_on_abandoned_attempt(self):
        old = self.reintroduce_release()
        text = (self.owner / "CHANGELOG.md").read_text()
        for version in ("v0.3.0", VERSION):
            (self.owner / "CHANGELOG.md").write_text(text.replace(VERSION, version))
            self.git(self.owner, "add", "CHANGELOG.md")
            self.git(self.owner, "commit", "-m", "Correct retry version " + version)
        self.git(self.clone, "fetch", "origin")
        self.git(self.clone, "checkout", "--detach", self.git(self.owner, "rev-parse", "HEAD"))
        self.assert_fails(report=EXISTS, status=1, publish=old, reason="does not match release")

    def test_reverting_unpublished_release_after_published_downward_correction(self):
        text = (self.owner / "CHANGELOG.md").read_text()
        for version in ("v0.3.0", "v0.2.0", "v0.2.1", "v0.2.0"):
            (self.owner / "CHANGELOG.md").write_text(text.replace(VERSION, version))
            self.git(self.owner, "add", "CHANGELOG.md")
            self.git(self.owner, "commit", "-m", "Version state " + version)
            if version == "v0.2.0" and not self.git(self.owner, "tag", "--list", version):
                self.git(self.owner, "tag", version)
        self.git(self.clone, "fetch", "--tags", "origin")
        self.git(self.clone, "checkout", "--detach", self.git(self.owner, "rev-parse", "HEAD"))
        calls = self.assert_passes(report="# summary\nSuggested version: v0.2.1\n")
        self.assertIn("-base=v0.2.0", calls[0])
        self.assertFalse(any(arg.startswith("-version=") for arg in calls[0]))

    def test_downward_version_typo_correction_remains_a_candidate(self):
        text = (self.owner / "CHANGELOG.md").read_text()
        for version in ("v0.3.0", "v0.2.0"):
            (self.owner / "CHANGELOG.md").write_text(text.replace(VERSION, version))
            self.git(self.owner, "add", "CHANGELOG.md")
            self.git(self.owner, "commit", "-m", "Correct unpublished version " + version)
        self.git(self.clone, "fetch", "origin")
        self.git(self.clone, "checkout", "--detach", self.git(self.owner, "rev-parse", "HEAD"))
        calls = self.assert_passes(report=VALID.replace(VERSION, "v0.2.0"))
        self.assertIn("-version=v0.2.0", calls[0])

    def test_legacy_pin_cannot_be_replaced_by_a_later_version_series_commit(self):
        for sha in api_check.LEGACY_RELEASES.values():
            self.assertFalse(api_check.matches_release("later", sha, "head", "v0.1.0"))

    def test_development_uses_newest_tag_when_changelog_lags(self):
        self.publish(fetch=True)
        self.advance_checkout()
        later = self.git(self.clone, "rev-parse", "HEAD")
        self.git(self.owner, "fetch", str(self.clone), later)
        self.git(self.owner, "tag", "v0.1.2", later)
        self.git(self.clone, "fetch", "--tags", "origin")
        calls = self.assert_passes(report="# summary\nSuggested version: v0.1.3\n")
        self.assertIn("-base=v0.1.2", calls[0])
        self.assertFalse(any(arg.startswith("-version=") for arg in calls[0]))

    def test_revert_of_unpublished_release_keeps_historical_release_informational(self):
        (self.owner / "CHANGELOG.md").write_text(self.git(self.owner, "show", self.previous + ":CHANGELOG.md") + "\n")
        self.git(self.owner, "add", "CHANGELOG.md")
        self.git(self.owner, "commit", "-m", "Revert unpublished release")
        self.git(self.clone, "fetch", "origin")
        self.git(self.clone, "checkout", "--detach", self.git(self.owner, "rev-parse", "HEAD"))
        calls = self.assert_passes(report="# summary\nSuggested version: v0.1.1\n")
        self.assertIn("-base=v0.1.0", calls[0])
        self.assertFalse(any(arg.startswith("-version=") for arg in calls[0]))

    def make_legacy_history(self):
        """Inject only fixture SHA data; all production Git/remote checks still execute."""
        old_root = self.previous
        self.git(self.owner, "checkout", "-B", "main", old_root)
        (self.owner / "CHANGELOG.md").write_text("# Changelog\n\n## [Unreleased]\n")
        (self.owner / "go.mod").write_text("module github.com/fin-assist/go-identity\n")
        self.git(self.owner, "add", ".")
        self.git(self.owner, "commit", "-m", "Legacy release without section")
        pinned = self.git(self.owner, "rev-parse", "HEAD")
        self.git(self.owner, "tag", "-f", "v0.1.0", pinned)
        (self.owner / "CHANGELOG.md").write_text("# Changelog\n\n## [Unreleased]\n\n"
                                               "## [v0.1.0] — 2026-10-24\n\n- Restored history.\n")
        self.git(self.owner, "add", ".")
        self.git(self.owner, "commit", "-m", "Record legacy release after publication")
        current = self.git(self.owner, "rev-parse", "HEAD")
        self.git(self.clone, "fetch", "--force", "--tags", "origin")
        self.git(self.clone, "checkout", "--detach", current)
        script = self.clone / "scripts/api_check.py"
        script.write_text(script.read_text().replace("f44f31941c19197cf92c007d4fe087b1d76557b0", pinned))
        return pinned, current, old_root

    def test_legacy_workflow_accepts_exact_published_history(self):
        self.make_legacy_history()
        calls = self.assert_passes(report="# summary\nSuggested version: v0.1.1\n")
        self.assertIn("-base=v0.1.0", calls[0])
        self.assertFalse(any(arg.startswith("-version=") for arg in calls[0]))

    def test_legacy_workflow_rejects_a_different_published_commit(self):
        _, current, _ = self.make_legacy_history()
        self.git(self.owner, "tag", "-f", "v0.1.0", current)
        self.git(self.clone, "fetch", "--force", "--tags", "origin")
        self.assertEqual(self.assert_fails(), [])

    def test_legacy_workflow_rejects_existing_pin_outside_checkout_history(self):
        _, _, old_root = self.make_legacy_history()
        self.git(self.clone, "checkout", "--detach", old_root)
        (self.clone / "go.mod").write_text("module github.com/fin-assist/go-identity\n")
        self.assertEqual(self.assert_fails(), [])

    def test_legacy_workflow_rejects_remote_deletion_before_checkout(self):
        self.make_legacy_history()
        self.git(self.owner, "tag", "-d", "v0.1.0")
        self.git(self.clone, "tag", "-d", "v0.1.0")
        calls = self.assert_fails(report=VALID.replace(VERSION, "v0.1.0"), reason="missing from origin")
        self.assertEqual(calls, [])

    def make_release_merge(self):
        self.git(self.owner, "checkout", "-B", "main", self.previous)
        self.git(self.owner, "merge", "--no-ff", "-m", "Merge release PR", self.head)
        self.head = self.git(self.owner, "rev-parse", "HEAD")
        self.git(self.clone, "fetch", "origin")
        self.git(self.clone, "checkout", "--detach", self.head)

    def test_release_merge_accepts_publication_on_merge_commit(self):
        self.make_release_merge()
        calls = self.assert_passes(report=EXISTS, status=1, publish=self.head, annotated=True)
        self.assertIn("-version=" + VERSION, calls[0])
        self.assertIn("-base=v0.1.0", calls[0])

    def test_pr_merge_after_release_merge_is_informational(self):
        self.make_release_merge()
        self.publish(fetch=True)
        self.git(self.owner, "checkout", "-b", "feature")
        (self.owner / "feature.txt").write_text("Ordinary PR\n")
        self.git(self.owner, "add", "feature.txt")
        self.git(self.owner, "commit", "-m", "Ordinary feature")
        self.git(self.owner, "checkout", "main")
        self.git(self.owner, "merge", "--no-ff", "-m", "Synthetic ordinary PR merge", "feature")
        self.git(self.clone, "fetch", "origin")
        self.git(self.clone, "checkout", "--detach", self.git(self.owner, "rev-parse", "HEAD"))
        calls = self.assert_passes(report="# summary\nSuggested version: v0.1.2\n")
        self.assertIn("-base=" + VERSION, calls[0])
        self.assertFalse(any(arg.startswith("-version=") for arg in calls[0]))


if __name__ == "__main__":
    unittest.main()
