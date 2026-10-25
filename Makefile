# Common commands; CI runs the same steps (.github/workflows/ci.yml).
SHELL := /bin/bash
GO_TEST_COVERAGE_VERSION := v2.20.1
TEST_TIMEOUT ?= 8m
COVERAGE := GOTOOLCHAIN=auto go run github.com/vladopajic/go-test-coverage/v2@$(GO_TEST_COVERAGE_VERSION)
GOLANGCI_LINT_VERSION := v2.15.0
GORELEASE := golang.org/x/exp/cmd/gorelease@v0.0.0-20261014143641-f0251eb4aa3e
export GOPRIVATE := github.com/fin-assist/*

.PHONY: test test-names cover collect-unit collect-integration collect-api check-unit check-integration check-api cover-unit cover-integration cover-api cover-combined lint fmt api release-check

test: test-names ## all Go race suites, script tests and changelog format
	go vet ./...
	go test -race -count=1 -timeout=$(TEST_TIMEOUT) ./...
	python3 -m unittest discover -s scripts -p 'test_*.py'
	python3 scripts/changelog.py check

test-names: ## reject tests accidentally omitted from the three selectors
	go test -race -list '^(Test|Fuzz|Example)' -timeout=8m ./... > test-names.txt
	@if grep -E '^(Test|Fuzz)' test-names.txt | grep -Ev '^(Test|Fuzz)(Unit|Integration|API).+'; then \
	  echo 'Name every test TestUnit..., TestIntegration... or TestAPI...' >&2; exit 1; fi

# All suites share Go's package inventory, excluding only the exact internal/pgtest harness package.
# Ordinary packages without tests may be reported as skipped; skipped Test events fail.
collect-unit collect-integration collect-api: test-names
	@set -euo pipefail; level='$(@:collect-%=%)'; case "$$level" in unit) selector='^(Test|Fuzz)Unit';; integration) selector='^(Test|Fuzz)Integration';; api) selector='^((Test|Fuzz)API|Example)';; esac; \
	rm -f "$$level.out" "$$level.jsonl"; \
	module=$$(go list -m); packages=$$(go list -race ./...); \
	coverpkgs=$$(printf '%s\n' "$$packages" | awk -v excluded="$$module/internal/pgtest" '$$0 != excluded' | paste -sd, -); \
	test -n "$$coverpkgs" || { echo 'No production packages remain for coverage.' >&2; exit 1; }; \
	CI=true go test -json -race -count=1 -timeout=8m -coverpkg="$$coverpkgs" \
	  -coverprofile="$$level.out" -run "$$selector" ./... | tee "$$level.jsonl"; \
	bash scripts/check-suite.sh "$$level.jsonl" "$$level.out"

check-unit check-integration check-api:
	@set -euo pipefail; level='$(@:check-%=%)'; \
	bash scripts/check-suite.sh "$$level.jsonl" "$$level.out"; \
	$(COVERAGE) --config .testcoverage.yml --profile "$$level.out" \
	  --breakdown-file-name "$$level.coverage.txt" | tee "$$level.report.txt"

cover-unit: collect-unit
	$(MAKE) check-unit
cover-integration: collect-integration
	$(MAKE) check-integration
cover-api: collect-api
	$(MAKE) check-api

cover-combined: ## informational union; the tool merges profiles, never percentages
	@set -euo pipefail; for level in unit integration api; do bash scripts/check-suite.sh "$$level.jsonl" "$$level.out"; done
	set -euo pipefail; $(COVERAGE) --profile unit.out,integration.out,api.out \
	  --breakdown-file-name combined.coverage.txt | tee combined.report.txt

cover: collect-unit collect-integration collect-api ## collect all profiles before enforcing the three gates
	@status=0; for level in unit integration api; do $(MAKE) check-$$level || status=1; done; \
	$(MAKE) cover-combined || status=1; exit "$$status"

lint: ## golangci-lint of the pinned version
	go run github.com/golangci/golangci-lint/v2/cmd/golangci-lint@$(GOLANGCI_LINT_VERSION) run ./...

fmt:
	go run github.com/golangci/golangci-lint/v2/cmd/golangci-lint@$(GOLANGCI_LINT_VERSION) fmt ./...

api: ## exported API against the latest tag and the suggested next version (informational)
	-go run $(GORELEASE) -base=$$(git tag --list 'v*' --sort=-v:refname | head -n1 | grep . || echo none)

release-check: ## VERSION=vX.Y.Z: gorelease accepts the version and CHANGELOG.md has its section
	@test -n "$(VERSION)" || { echo "usage: make release-check VERSION=vX.Y.Z"; exit 2; }
	python3 scripts/changelog.py check
	test "$$(python3 scripts/changelog.py latest)" = "$(VERSION)"
	go run $(GORELEASE) -base=$$(git tag --list 'v*' --sort=-v:refname | head -n1 | grep . || echo none) -version=$(VERSION) \
		| tee /dev/stderr | grep -q '^$(VERSION) is a valid semantic version for this release'
