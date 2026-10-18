# Common commands; CI runs the same steps (.github/workflows/ci.yml).
GOLANGCI_LINT_VERSION := v2.15.0
GORELEASE := golang.org/x/exp/cmd/gorelease@v0.0.0-20261014143641-f0251eb4aa3e
export GOPRIVATE := github.com/fin-assist/*

.PHONY: test cover lint fmt api release-check

test: ## unit and integration tests (Docker or TEST_DATABASE_URL), script tests, changelog format
	go vet ./...
	go test -race -count=1 ./...
	python3 -m unittest discover -s scripts -p 'test_*.py'
	python3 scripts/changelog.py check

cover: ## tests with a coverage profile, then coverage of the lines changed against origin/main
	go test -race -count=1 -coverpkg=./... -coverprofile=cover.out ./...
	python3 scripts/coverage-diff.py --base origin/main

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
