#!/usr/bin/env python3
"""Project coverage and coverage of the lines a branch changes (README.md, «Покрытие изменённых строк»).

Reads a Go coverage profile (`go test -coverpkg=./... -coverprofile=cover.out ./...`) and the diff of the
branch against its base, and reports how many of the changed executable lines of production code the tests
run. A changed line counts if a nonzero-statement coverage block contains it. This is a line-span metric:
comments and blank lines inside a block count too. Tests and testdata files are excluded.

Usage:
  make cover                                                     # writes cover.out
  python3 scripts/coverage-diff.py --base origin/main            # Markdown to stdout
  python3 scripts/coverage-diff.py --base origin/main --min 80   # exit 1 below 80 %
  python3 scripts/coverage-diff.py --base … --summary "$GITHUB_STEP_SUMMARY"
  python3 scripts/coverage-diff.py                               # project totals only (no diff)

This identical script is shared by the two Go templates and existing repositories. Root-module CI uses
one profile; contracts explicitly passes gen/go and tools, and the empty bank-bff scaffold uses --empty.
All explicitly supplied paths are relative to the current directory; defaults point at the repository root.
Standard library only, so CI runs it without pip.
"""

from __future__ import annotations

import argparse
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROFILE = ROOT / "cover.out"

_BLOCK = re.compile(r"^(.+\.go):(\d+)\.\d+,(\d+)\.\d+ (\d+) (\d+)$")
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_MODULE = re.compile(r"^module\s+(\S+)", re.M)


# --- parsing: pure functions over text (scripts/test_coverage_diff.py) ---------------------------------------

def parse_profile(text: str, module: str) -> dict[str, dict[int, bool]]:
    """Coverage profile → {repo path: {line: covered}} for every line inside a block.

    With -coverpkg the same block appears once per test binary; a line is covered if any entry ran it.
    Files outside the module (none expected) are skipped."""
    lines: dict[str, dict[int, bool]] = {}
    prefix = module.rstrip("/") + "/"
    for raw in text.splitlines():
        m = _BLOCK.match(raw.strip())
        if not m:
            continue  # "mode: atomic" header, blank lines
        path, start, end, stmts, count = m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5))
        if not path.startswith(prefix) or not stmts:
            continue
        file = lines.setdefault(path[len(prefix):], {})
        for n in range(start, end + 1):
            file[n] = file.get(n, False) or count > 0
    return lines


def is_production(path: str) -> bool:
    """Production Go, including generated code; tests and testdata are excluded."""
    return path.endswith(".go") and not path.endswith("_test.go") and "/testdata/" not in f"/{path}"


def changed_lines(diff: str) -> dict[str, set[int]]:
    """`git diff -U0` → {repo path: added or modified line numbers in the new file} for production Go files."""
    out: dict[str, set[int]] = {}
    current: str | None = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            name = line[4:].strip()
            current = name[2:] if name.startswith("b/") else None  # "/dev/null" for a deleted file
            if current is not None and not is_production(current):
                current = None
            continue
        m = _HUNK.match(line)
        if m and current is not None:
            start, count = int(m.group(1)), int(m.group(2) or "1")
            out.setdefault(current, set()).update(range(start, start + count))
    return {k: v for k, v in out.items() if v}


@dataclass
class FileResult:
    path: str
    covered: int = 0
    total: int = 0
    uncovered: list[int] = field(default_factory=list)

    @property
    def pct(self) -> float:
        return 100.0 * self.covered / self.total if self.total else 100.0


def measure(changed: dict[str, set[int]], profile: dict[str, dict[int, bool]]) -> list[FileResult]:
    """Changed executable lines per file. A changed file missing from the profile (a package without tests is
    still in the profile thanks to -coverpkg) is skipped: it has no executable lines."""
    results = []
    for path in sorted(changed):
        blocks = profile.get(path, {})
        r = FileResult(path)
        for n in sorted(changed[path]):
            if n not in blocks:
                continue
            r.total += 1
            if blocks[n]:
                r.covered += 1
            else:
                r.uncovered.append(n)
        if r.total:
            results.append(r)
    return results


def ranges(nums: list[int]) -> str:
    """[3, 4, 5, 9] → "3–5, 9"."""
    parts, start, prev = [], None, None
    for n in nums:
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            parts.append(f"{start}" if start == prev else f"{start}–{prev}")
            start = prev = n
    if start is not None:
        parts.append(f"{start}" if start == prev else f"{start}–{prev}")
    return ", ".join(parts)


def totals(profile: dict[str, dict[int, bool]]) -> tuple[int, int]:
    covered = total = 0
    for path, lines in profile.items():
        if not is_production(path):
            continue
        total += len(lines)
        covered += sum(lines.values())
    return covered, total


def render_diff(results: list[FileResult], base: str, minimum: float | None, available: bool = True) -> str:
    covered = sum(r.covered for r in results)
    total = sum(r.total for r in results)
    out = ["### Покрытие изменённых строк", ""]
    if not available:
        return "\n".join(out) + "Нет данных покрытия production-кода; изменённые исполняемые строки не определены.\n"
    if not total:
        out.append(f"Против `{base}` нет изменённых исполняемых строк production-кода.")
        return "\n".join(out) + "\n"
    pct = 100.0 * covered / total
    goal = f" (цель — не ниже {minimum:g} %)" if minimum is not None else ""
    out.append(f"**{pct:.1f} %** — {covered} из {total} изменённых исполняемых строк против `{base}`{goal}.")
    out += ["", "| Файл | Покрыто | Не покрыты строки |", "|---|---|---|"]
    for r in sorted(results, key=lambda r: (r.pct, r.path)):
        out.append(f"| `{r.path}` | {r.covered}/{r.total} ({r.pct:.0f} %) | {ranges(r.uncovered) or '—'} |")
    return "\n".join(out) + "\n"


def render_totals(covered: int, total: int) -> str:
    if not total:
        return "### Покрытие проекта\n\nНет данных: 0 исполняемых строк production-кода в профиле покрытия.\n"
    pct = 100.0 * covered / total
    return f"### Покрытие проекта\n\n**{pct:.1f} %** — {covered} из {total} исполняемых строк production-кода.\n"


# --- I/O -----------------------------------------------------------------------------------------------------

def module_path(directory: Path = ROOT) -> str:
    m = _MODULE.search((directory / "go.mod").read_text())
    if not m:
        sys.exit("go.mod has no module line")
    return shlex.split(m.group(1))[0]


def load_profiles(inputs: list[tuple[Path, Path]]) -> dict[str, dict[int, bool]]:
    """Merge module profiles under repository-relative paths without mixing module namespaces."""
    profile = {}
    for directory, source in inputs:
        directory = directory.resolve()
        prefix = directory.relative_to(ROOT.resolve()).as_posix()
        for path, lines in parse_profile(source.read_text(), module_path(directory)).items():
            name = path if prefix == "." else f"{prefix}/{path}"
            target = profile.setdefault(name, {})
            for number, covered in lines.items():
                target[number] = target.get(number, False) or covered
    return profile


def git_diff(base: str) -> str:
    # base...HEAD: changes of the branch since it left the base, not what the base got meanwhile.
    return subprocess.run(
        ["git", "-c", "core.quotepath=off", "diff", "-U0", "--no-color", "--no-ext-diff", f"{base}...HEAD", "--", "*.go"],
        cwd=ROOT, check=True, capture_output=True, text=True,
    ).stdout


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    inputs_group = ap.add_mutually_exclusive_group()
    inputs_group.add_argument("--profile", type=Path, default=PROFILE, help="coverage profile (default: repository cover.out)")
    inputs_group.add_argument("--module-profile", nargs=2, action="append", metavar=("MODULE_DIR", "PROFILE"),
                    help="module directory and profile, both relative to cwd; repeat for multiple modules")
    inputs_group.add_argument("--empty", action="store_true", help="explicit report for a scaffold without a Go module")
    ap.add_argument("--base", help="git ref to diff against, e.g. origin/main; without it, project totals")
    ap.add_argument("--min", type=float, help="exit 1 when changed-line coverage is below this percentage")
    ap.add_argument("--summary", type=Path, help="also append the Markdown to this file")
    args = ap.parse_args(argv)
    if args.min is not None and not args.base:
        ap.error("--min needs --base")

    if args.empty and any(ROOT.rglob("go.mod")):
        ap.error("--empty is only for a scaffold without a Go module")
    inputs = [(Path(directory), Path(source)) for directory, source in args.module_profile] if args.module_profile else [(ROOT, args.profile)]
    try:
        profile = {} if args.empty else load_profiles(inputs)
    except (ValueError, OSError) as error:
        ap.error(str(error))
    project_covered, project_total = totals(profile)
    text = render_totals(project_covered, project_total)
    if args.base:
        results = measure(changed_lines(git_diff(args.base)), profile)
        text += "\n" + render_diff(results, args.base, args.min, available=project_total > 0)

    sys.stdout.write(text)
    if args.summary:
        with args.summary.open("a") as f:
            f.write(text + "\n")
    if args.min is not None:
        # --min requires --base: the target uses changed lines only, never project totals.
        covered, total = sum(r.covered for r in results), sum(r.total for r in results)
        if not project_total or (total and 100.0 * covered / total < args.min):
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
