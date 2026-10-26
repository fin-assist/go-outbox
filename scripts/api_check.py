#!/usr/bin/env python3
"""Run the pinned gorelease check without racing owner-published release tags (FIN-264)."""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys

import changelog


def git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True,
                          text=True, timeout=60).stdout.strip()


def remote_commit(version: str) -> str:
    """Read exact refs without fetching/mutating tags; peel annotated tags."""
    ref = "refs/tags/" + version
    lines = git("ls-remote", "--tags", "origin", ref, ref + "^{}")
    refs = dict(line.split()[::-1] for line in lines.splitlines())
    return refs.get(ref + "^{}", refs.get(ref, ""))


def verdict(report: str, version: str) -> str:
    """Recognise only the pinned tool's complete summary, never an error substring.

    Dependency diagnostics can give go run status 1 even with a valid version.
    Package/base errors are not eligible for the publication exception. Any tool
    upgrade that changes this format must update the regression fixtures too.
    """
    if "## errors in " in report:
        return "invalid"
    parts = re.split(r"(?m)^# summary\n", report)
    if len(parts) != 2:
        return "invalid"
    summary = parts[1].strip()
    if summary.endswith("\nexit status 1"):
        summary = summary[:-len("\nexit status 1")].rstrip()
    if summary == f"{version} is a valid semantic version for this release.":
        return "valid"
    if summary == (f"{version} is not a valid semantic version for this release.\n"
                   f"version {version} already exists"):
        return "exists"
    return "invalid"


def check() -> None:
    next_version = changelog.latest(changelog.parse(Path("CHANGELOG.md").read_text()))
    head = git("rev-parse", "HEAD")
    tags = git("tag", "--list", "v*", "--sort=-v:refname").splitlines()
    local = git("rev-parse", f"refs/tags/{next_version}^{{commit}}") if next_version in tags else ""
    before = remote_commit(next_version) if next_version else ""
    # A known historical release remains the informational base for subsequent
    # development. A missing local tag or a tag at HEAD means a release check.
    candidate = bool(next_version) and (not local or local == head)
    if local and before != local:
        raise ValueError("Local and remote release tags disagree (or the remote tag is missing).")
    if candidate and before and before != head:
        raise ValueError("The published release tag does not identify the checked release commit.")
    if local and not candidate:
        git("merge-base", "--is-ancestor", local, head)
    bases = [tag for tag in tags if not candidate or tag != next_version]
    base = bases[0] if bases else "none"
    args = [f"-base={base}"]
    if candidate:
        args.append(f"-version={next_version}")
    # bash resolves an exported go function in the offline regression harness.
    # Arguments are positional, never interpolated into shell source.
    result = subprocess.run(
        ["bash", "-c", 'go "$@"', "api-check",
         "run", os.environ["GORELEASE"], *args],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=600)
    report = result.stdout
    Path("gorelease.txt").write_text(report)
    print(report, end="")
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a") as summary:
            summary.write(f"### API против {base}\n\n```text\n{report}\n```\n")
    after = remote_commit(next_version) if next_version else ""
    if before and after != before:
        raise ValueError("The remote release tag changed during the check.")
    if candidate:
        if after and after != head:
            raise ValueError("The published release tag does not identify the checked release commit.")
        outcome = verdict(report, next_version)
        if result.returncode not in (0, 1) or outcome == "invalid":
            raise ValueError("gorelease did not accept the CHANGELOG version.")
        if outcome == "exists" and after != head:
            raise ValueError("An existing version is accepted only with its remote tag at the release commit.")
        if outcome == "exists":
            print(f"Verified published {next_version} at {head}; API compared against {base}.")


if __name__ == "__main__":
    try:
        check()
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        print(f"API check failed: {error}", file=sys.stderr)
        sys.exit(1)
