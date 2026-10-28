#!/usr/bin/env python3
"""Resolve the immutable shared CI source; copied unchanged into consumer scripts/."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile

SOURCE_URL = "https://github.com/fin-assist/go-service-template.git"
CONFIG_NAME = ".finassist-ci.json"


def unique_object(pairs):
    """Reject ambiguous manifests rather than silently accepting duplicate keys."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate shared CI configuration key.")
        result[key] = value
    return result


def read_config(root: Path) -> dict:
    """Read the small allowlisted consumer manifest without accepting shell syntax."""
    path = root / CONFIG_NAME
    if path.stat().st_size > 4096:
        raise ValueError("Shared CI configuration is too large.")
    config = json.loads(path.read_text(), object_pairs_hook=unique_object)
    if not isinstance(config, dict) or set(config) - {"revision", "profile", "gosec_g115", "squawk_smallint"}:
        raise ValueError("Unknown shared CI configuration fields.")
    if not isinstance(config.get("revision"), str) or not re.fullmatch(r"[0-9a-f]{40}", config["revision"]):
        raise ValueError("Shared CI revision must be a full lowercase commit SHA.")
    if config.get("profile") not in ("service", "library"):
        raise ValueError("Shared CI profile must be service or library.")
    for key in ("gosec_g115", "squawk_smallint"):
        if key in config and type(config[key]) is not bool:
            raise ValueError("Shared CI overrides must be booleans.")
        config.setdefault(key, False)
    return config


def git_output(root: Path, args: tuple[str, ...], text: bool):
    """Read bounded Git output without shell, hooks, prompts or replacement objects."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0",
               GIT_NO_REPLACE_OBJECTS="1")
    try:
        result = subprocess.run(
            ["git", "-c", "core.worktree=" + str(root.resolve()), "-c", "core.hooksPath=" + os.devnull,
             "-c", "core.fsmonitor=false", "-c", "protocol.file.allow=never", *args],
            cwd=root, env=env, check=True, capture_output=True, text=text, timeout=120)
    except (subprocess.SubprocessError, OSError) as error:
        # Git stderr can contain local credential helpers or authenticated URL rewrites.
        raise ValueError("Unable to obtain or verify the pinned shared CI source.") from error
    return result.stdout


def git(root: Path, *args: str) -> str:
    """Run a bounded textual Git command using the explicit caller working tree."""
    return git_output(root, args, True).strip()


def object_bytes(cache: Path, kind: str, oid: str) -> bytes:
    """Verify an object's raw bytes against its SHA-1 identity, not local Git metadata."""
    if not re.fullmatch(r"[0-9a-f]{40}", oid):
        raise ValueError("Invalid pinned Git object identity.")
    data = git_output(cache, ("cat-file", kind, oid), False)
    header = kind.encode() + b" " + str(len(data)).encode() + b"\0"
    if hashlib.sha1(header + data).hexdigest() != oid:
        raise ValueError("Pinned Git object integrity check failed.")
    return data


def verify_checkout(cache: Path, revision: str) -> None:
    """Compare raw files with the authenticated commit tree without clean filters/stat caches."""
    commit = object_bytes(cache, "commit", revision)
    first = commit.split(b"\n", 1)[0]
    if not re.fullmatch(b"tree [0-9a-f]{40}", first):
        raise ValueError("Pinned commit has no valid root tree.")
    object_bytes(cache, "tree", first[5:].decode("ascii"))
    # ls-tree only reads objects; unlike status/diff it never runs a worktree clean filter.
    records = git_output(cache, ("ls-tree", "-r", "-t", "-z", "--full-tree", revision), False)
    expected = {}
    for record in records.split(b"\0"):
        if not record:
            continue
        header, separator, raw_path = record.partition(b"\t")
        if not separator:
            raise ValueError("Invalid pinned tree listing.")
        mode, kind, raw_oid = header.split(b" ")
        oid = raw_oid.decode("ascii")
        path = PurePosixPath(os.fsdecode(raw_path))
        if path.is_absolute() or any(part in {".", ".."} or part.lower() == ".git" or "\\" in part for part in path.parts):
            raise ValueError("Unsafe path in pinned source tree.")
        if mode == b"040000" and kind == b"tree":
            object_bytes(cache, "tree", oid)
        elif mode in {b"100644", b"100755"} and kind == b"blob":
            expected[path.as_posix()] = oid
        else:
            raise ValueError("Pinned source must contain regular files and directories only.")
    actual = set()
    for directory, dirs, files in os.walk(cache, followlinks=False):
        parent = Path(directory)
        if parent == cache:
            dirs.remove(".git")
        if any((parent / name).is_symlink() for name in dirs + files):
            raise ValueError("Shared CI cache has local modifications (symbolic link).")
        actual.update((parent / name).relative_to(cache).as_posix() for name in files)
    if actual != set(expected):
        raise ValueError("Shared CI cache has local modifications (file inventory).")
    for name, oid in expected.items():
        path = cache / name
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Shared CI cache has local modifications (non-regular file).")
        size = info.st_size
        digest = hashlib.sha1(b"blob " + str(size).encode() + b"\0")
        count = 0
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(65536), b""):
                digest.update(chunk)
                count += len(chunk)
        if count != size or digest.hexdigest() != oid:
            raise ValueError("Shared CI cache has local modifications (file content).")


def validate_cache(cache: Path, revision: str) -> Path:
    """Reject modified working trees and incorrect origin or commit identities."""
    if cache.is_symlink() or not cache.is_dir() or not (cache / ".git").is_dir() or (cache / ".git").is_symlink():
        raise ValueError("Invalid shared CI cache directory.")
    if git(cache, "rev-parse", "HEAD") != revision:
        raise ValueError("Shared CI cache revision differs from the manifest.")
    if git(cache, "config", "--get", "remote.origin.url") != SOURCE_URL:
        raise ValueError("Shared CI cache has an unexpected source repository.")
    if any(line.startswith(tuple("abcdefghijklmnopqrstuvwxyzS")) for line in git(cache, "ls-files", "-v").splitlines()):
        raise ValueError("Shared CI cache has hidden index entries; remove that revision cache and retry.")
    verify_checkout(cache, revision)
    common = cache / "common"
    if common.is_symlink() or not (common / "render_lint.py").is_file():
        raise ValueError("Pinned shared CI source is incomplete.")
    return common


def source_dir(root: Path, config: dict, source: Path | None = None) -> Path:
    """Resolve an explicit local source or an independently verified pinned checkout."""
    if source is not None:
        if os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("GITHUB_EVENT_NAME") not in {"push", "pull_request"}:
            raise ValueError("Local source overrides are restricted to ordinary push or pull_request CI.")
        common = source.resolve()
        if not common.is_dir() or not (common / "render_lint.py").is_file() or not (common / "configs").is_dir():
            raise ValueError("Local source must be the shared common directory.")
        return common
    cache_root = root / ".finassist-ci"
    if cache_root.is_symlink():
        raise ValueError("Shared CI cache root must not be a symbolic link.")
    cache_root.mkdir(exist_ok=True)
    revision = config["revision"]
    cache = cache_root / revision
    if cache.exists() or cache.is_symlink():
        return validate_cache(cache, revision)
    staging = Path(tempfile.mkdtemp(prefix=".fetch-", dir=cache_root))
    try:
        template = staging / "empty-template"
        template.mkdir()
        git(staging, "init", "--quiet", "--template=" + str(template))
        template.rmdir()
        git(staging, "remote", "add", "origin", SOURCE_URL)
        git(staging, "fetch", "--quiet", "--depth=1", "origin", revision)
        git(staging, "checkout", "--quiet", "--detach", "FETCH_HEAD")
        validate_cache(staging, revision)
        try:
            staging.rename(cache)
        except OSError:
            if not cache.is_dir():
                raise
        return validate_cache(cache, revision)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def main() -> int:
    """Print the common directory, or render shared configs for the caller root."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=["render"])
    parser.add_argument("--source", type=Path, help="Explicit local common/ source for development and unprivileged CI")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    try:
        common = source_dir(root, read_config(root), args.source)
        if args.command == "render":
            subprocess.run([sys.executable, "-B", str(common / "render_lint.py"), "--root", str(root)],
                           check=True, timeout=60)
        else:
            print(common)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print("Shared CI bootstrap failed: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
