#!/usr/bin/env python3
"""Run the pinned gorelease check without racing owner-published release tags (FIN-264)."""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys

import changelog


# Published tags whose immutable trees predate their CHANGELOG sections.
# Audited against origin on 2026-10-26; exact module/version/SHA only.
LEGACY_RELEASES = {
    ("github.com/fin-assist/go-grpc", "v0.2.0"): "47193ac83a3e1c35a678166f2147e98abb2d9322",
    ("github.com/fin-assist/go-idempotency", "v0.1.0"): "be968e2dc4d50ed3577d75b91e6ee58aad55b420",
    ("github.com/fin-assist/go-idempotency", "v0.2.0"): "67fc00cc91064d883f452c3982fdcb310b7e4680",
    ("github.com/fin-assist/go-idempotency", "v0.3.0"): "aaad7ad3aaff61b9a2c9e13681ff82ccf7ee9b11",
    ("github.com/fin-assist/go-idempotency", "v0.4.0"): "9be196399c8f55c737fc83274e64b36f4d7d225a",
    ("github.com/fin-assist/go-idempotency", "v0.5.0"): "9fa6de5d6cf4cefe1ba8bf8b346ef21bb1a0932f",
    ("github.com/fin-assist/go-identity", "v0.1.0"): "f44f31941c19197cf92c007d4fe087b1d76557b0",
    ("github.com/fin-assist/go-identity", "v0.2.0"): "ec005cfecb4fcbaf4e10b9bdc1d6eebfbaff950b",
    ("github.com/fin-assist/go-migrate", "v0.2.0"): "16bdd5875857fa7a5f2ada9672b9006ab15a60d7",
    ("github.com/fin-assist/go-outbox", "v0.1.0"): "d5043a4e5791338eb1d3c3ae0ba07d4bfb95a402",
    ("github.com/fin-assist/go-outbox", "v0.2.0"): "175cf7b94f2ad6c85759fe8c5993cf0b4d9dc203",
    ("github.com/fin-assist/go-postgres", "v0.1.0"): "1f66a939db86e8318d872142bee17c8844841478",
    ("github.com/fin-assist/go-postgres", "v0.1.2"): "811ad21f373b4d753a7b2c3de503d295f49ce176",
    ("github.com/fin-assist/go-postgres", "v0.3.0"): "69e396a55c19b956ccb9b4b793107c8aa7697554",
    ("github.com/fin-assist/go-tombstone", "v0.1.0"): "0d6edd6ca9419c99bcb490cdb732c962ba6887d6",
    ("github.com/fin-assist/go-worker", "v0.1.1"): "488cf47b04fe95492a620c8ba245c09542bdec26",
}


def git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True,
                          text=True, timeout=60).stdout.strip()


def remote_commit(version: str) -> str:
    """Read exact refs without fetching/mutating tags; peel annotated tags."""
    ref = "refs/tags/" + version
    lines = git("ls-remote", "--tags", "origin", ref, ref + "^{}")
    refs = dict(line.split()[::-1] for line in lines.splitlines())
    return refs.get(ref + "^{}", refs.get(ref, ""))


def version_at(commit: str) -> str:
    """The first parent can predate the initial CHANGELOG."""
    if not git("ls-tree", "--name-only", commit, "--", "CHANGELOG.md"):
        return ""
    return changelog.latest(changelog.parse(git("show", f"{commit}:CHANGELOG.md")))


def release_commit(version: str, published: str = "") -> str:
    """Locate the release's introduction on main/PR first-parent history.

    Local tag availability cannot distinguish a release from later development:
    its owner may publish the tag after either checkout. A release merge is the
    first-parent commit that introduces the version, not its preparation commit.
    """
    module_file = Path("go.mod")
    module = re.search(r"(?m)^module\s+(\S+)\s*$", module_file.read_text()) if module_file.exists() else None
    legacy = LEGACY_RELEASES.get((module[1], version)) if module else None
    if legacy:
        git("merge-base", "--is-ancestor", legacy, "HEAD")
        return legacy
    # A published immutable tag selects its historical same-version segment.
    # Without publication, only the current segment may become the release:
    # an abandoned earlier attempt must not validate a retry (even after typos).
    commits = git("rev-list", "--first-parent", "HEAD").splitlines()
    start = commits.index(published) if published in commits and version_at(published) == version else 0
    if version_at(commits[start]) != version:
        raise ValueError(f"CHANGELOG version {version} is absent from checked first-parent history.")
    release = commits[start]
    for parent in commits[start + 1:]:
        if version_at(parent) != version:
            break
        release = parent
    return release


def matches_release(commit: str, release: str, head: str, version: str) -> bool:
    """Validate exact identity on the checked first-parent release history.

    The canonical release commit and historical pins require exact identity.
    A later checked commit may recognize its own same-version first-parent
    series, but never a PR preparation commit from the second parent.
    """
    if commit == release:
        return True
    if head == release or release in LEGACY_RELEASES.values():
        return False
    commits = git("rev-list", "--first-parent", "HEAD").splitlines()
    series = commits[:commits.index(release)]
    return commit in series and version_at(commit) == version


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
    release = release_commit(next_version, before) if next_version else head
    if release in LEGACY_RELEASES.values() and not before:
        raise ValueError(f"Published legacy tag {next_version} is missing from origin; expected {release}.")
    # Preserve the original gate for every unpublished version, including
    # development commits that might become the owner's eventual tag target.
    validate_version = bool(next_version) and (not before or before == head or release == head)
    if local and before != local:
        raise ValueError("Local and remote release tags disagree (or the remote tag is missing).")
    if before and not matches_release(before, release, head, next_version):
        raise ValueError(f"Published {next_version} at {before} does not match release {release} (HEAD {head}).")
    bases = [tag for tag in tags if not validate_version or tag != next_version]
    if validate_version and before == head:
        # A historical release rerun can see tags published after its checkout
        # commit. Its compatibility base is still the preceding release.
        bases = [tag for tag in bases if changelog.version_key(tag) < changelog.version_key(next_version)]
    base = bases[0] if bases else "none"
    if not validate_version and before and (not bases or changelog.version_key(next_version) > changelog.version_key(base)):
        base = next_version
    args = [f"-base={base}"]
    if validate_version:
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
        raise ValueError(f"Remote {next_version} changed from {before} to {after or 'missing'} (release {release}).")
    if after and not matches_release(after, release, head, next_version):
        raise ValueError(f"Published {next_version} at {after} does not match release {release} (HEAD {head}).")
    if validate_version:
        outcome = verdict(report, next_version)
        if result.returncode not in (0, 1) or outcome == "invalid":
            raise ValueError("gorelease did not accept the CHANGELOG version.")
        if outcome == "exists" and not after:
            raise ValueError("An existing version is accepted only with its remote tag at the release commit.")
        if outcome == "exists":
            print(f"Verified published {next_version} at {after}; checked HEAD {head} against {base}.")


if __name__ == "__main__":
    try:
        check()
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        print(f"API check failed: {error}", file=sys.stderr)
        sys.exit(1)
