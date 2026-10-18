#!/usr/bin/env python3
"""Versions and release notes from CHANGELOG.md (docs/releasing.md).

  python3 scripts/changelog.py latest          # the newest released version, e.g. v0.2.0 (empty if none)
  python3 scripts/changelog.py notes v0.2.0    # the body of that section, for the GitHub release
  python3 scripts/changelog.py check           # the file follows the format; exit 1 otherwise

The file has an "## [Unreleased]" section on top, then released sections newest first:
"## [vX.Y.Z] — YYYY-MM-DD". Standard library only, so CI runs it without pip.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = ROOT / "CHANGELOG.md"

_HEADING = re.compile(r"^## \[(?P<name>[^\]]+)\](?:\s+[—-]\s+(?P<date>\S+))?\s*$")
_VERSION = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass
class Section:
    name: str  # "Unreleased" or a version
    date: str | None
    line: int  # 1-based line of the heading
    body: str


def parse(text: str) -> list[Section]:
    """Splits the file into "## [...]" sections in file order."""
    sections: list[Section] = []
    body: list[str] = []
    for i, line in enumerate(text.splitlines(), start=1):
        m = _HEADING.match(line)
        if m:
            if sections:
                sections[-1].body = "\n".join(body).strip()
            sections.append(Section(m["name"], m["date"], i, ""))
            body = []
        elif sections:
            body.append(line)
    if sections:
        sections[-1].body = "\n".join(body).strip()
    return sections


def version_key(v: str) -> tuple[int, int, int]:
    m = _VERSION.match(v)
    if not m:
        raise ValueError(f"not a version: {v}")
    return int(m[1]), int(m[2]), int(m[3])


def problems(sections: list[Section]) -> list[str]:
    """What breaks the format: no Unreleased on top, bad versions or dates, wrong order, repeats."""
    out: list[str] = []
    if not sections or sections[0].name != "Unreleased":
        out.append('the first section must be "## [Unreleased]"')
    seen: set[str] = set()
    previous: tuple[int, int, int] | None = None
    for s in sections:
        if s.name == "Unreleased":
            if s is not sections[0]:
                out.append(f'line {s.line}: "Unreleased" must be the first section')
            continue
        if not _VERSION.match(s.name):
            out.append(f"line {s.line}: {s.name!r} is not a version vX.Y.Z")
            continue
        if s.date is None or not _DATE.match(s.date):
            out.append(f"line {s.line}: {s.name} needs a date: ## [{s.name}] — YYYY-MM-DD")
        if s.name in seen:
            out.append(f"line {s.line}: {s.name} appears twice")
        seen.add(s.name)
        key = version_key(s.name)
        if previous is not None and key >= previous:
            out.append(f"line {s.line}: {s.name} is not older than the section above it")
        previous = key
        if not s.body:
            out.append(f"line {s.line}: {s.name} has no entries")
    return out


def latest(sections: list[Section]) -> str:
    for s in sections:
        if _VERSION.match(s.name):
            return s.name
    return ""


def notes(sections: list[Section], version: str) -> str | None:
    for s in sections:
        if s.name == version:
            return s.body
    return None


def main(argv: list[str]) -> int:
    sections = parse(CHANGELOG.read_text(encoding="utf-8"))
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "latest" and len(argv) == 2:
        print(latest(sections))
        return 0
    if cmd == "notes" and len(argv) == 3:
        body = notes(sections, argv[2])
        if body is None:
            print(f"CHANGELOG.md has no section for {argv[2]}", file=sys.stderr)
            return 1
        print(body)
        return 0
    if cmd == "check" and len(argv) == 2:
        found = problems(sections)
        for p in found:
            print(f"CHANGELOG.md: {p}", file=sys.stderr)
        return 1 if found else 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
