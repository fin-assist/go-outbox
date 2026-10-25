#!/usr/bin/env bash
# Validate execution evidence only; go-test-coverage owns all coverage calculations.
set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo 'usage: check-suite.sh TEST_JSONL COVER_PROFILE' >&2
  exit 2
fi

if ! jq -e -s '
  any(.[]; .Action == "pass" and has("Test")) and
  all(.[]; .Action != "fail" and (.Action != "skip" or (has("Test") | not)))
' "$1" > /dev/null; then
  echo 'Suite must execute and pass at least one test, with no skips or failures.' >&2
  exit 1
fi

# Reject absent, empty, header-only and zero-statement profiles, without computing a ratio.
if ! grep -Eq '^[^[:space:]]+:[0-9]+\.[0-9]+,[0-9]+\.[0-9]+[[:space:]]+[1-9][0-9]*[[:space:]]+[0-9]+$' "$2"; then
  echo 'Coverage profile must contain a block with executable statements.' >&2
  exit 1
fi
