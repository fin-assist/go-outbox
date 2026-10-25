# Instructions for code agents

The `outbox` Go library of «Понятные финансы» (B2C personal finance: OFX statement import from T-Bank,
operations, analytics, LLM assistant), FIN-109, made from `fin-assist/go-library-template`. The Go services
(made from `fin-assist/go-service-template`) import it at a tagged version. Read by Claude Code (through
`CLAUDE.md`) and by Codex. This file is the orientation map: read it and `README.md` first, then open only what
the task touches.

## Keep this file current

A PR that changes what this file, `README.md`, `CHANGELOG.md` or `docs/` say (layout, API, commands, CI checks,
conventions, process) updates them in the same PR. A library made from the template rewrites the first paragraph
for itself: what it does, its YouTrack issue, which services use it.

## Where things are decided

- Contracts: `fin-assist/contracts` — `docs/grpc-api.md` (call models, metadata, errors, idempotency, outbox,
  identity), `docs/api.md` (REST), `proto/`. A library that builds client-facing errors imports
  `github.com/fin-assist/contracts/gen/go` (`finerr`) at a tag `gen/go/vX.Y.Z`. A library never defines its own
  `.proto`, error codes or metadata keys: a change goes to `contracts` first (its own FIN issue and PR).
- Architecture: Confluence PF (https://finassist.atlassian.net/wiki/spaces/PF), «Архитектура» («Репозитории и
  структура кода», «Миграции и доступ к БД», and the section of the library's topic). The library's own issue
  (FIN-78, FIN-109, FIN-110, FIN-119, …) holds its scope; versions and releases — `docs/releasing.md` (FIN-118).
- Every change goes through a YouTrack issue (`FIN-<n>`, finassist.youtrack.cloud) referenced in the PR; Renovate
  updates without changes to the library need none (`docs/releasing.md`).
- Mismatches between the contracts, Confluence and this repository are expected. Resolve them in favour of the
  contracts and the owner's latest decision, say so in the PR, and propose (not impose) engineering changes. The
  owner writes backend code himself and is strong in PostgreSQL: schemas and queries are proposed with arguments,
  not decided for him.

## Repository map

| What | Where |
|---|---|
| Package documentation: what the library does, what it needs from the service | `doc.go` |
| Public API of the library | `outbox.go` (transactional publisher), `dispatcher.go` (routes and primary worker), `grpc.go` (system RPC adapter) |
| Implementation details | `primary.go` (private pool/connection operations), `internal/`; dispatcher connection-lease seam and its `go-postgres.Manager` — `dispatcher.go` |
| Migration source for the service runner (`Migrations`, `MigrationsName`, `MigrationsTable`) | `migrations.go`, `migrations/` |
| PostgreSQL for tests, up → down → up check | `internal/pgtest/`: only server selection; databases, file rules and the walk delegate to the tagged `go-migrate` (`migratetest`, `migrate.Validate`, `migrate.Up`; FIN-318) |
| Integration guide for services | `README.md`, «Внедрение» |
| Versions, compatibility, releases, Renovate | `docs/releasing.md`, `CHANGELOG.md`, `.github/workflows/release.yml`, `renovate.json` |
| Suite execution/profile guards and native coverage policy, changelog checks, lint policy probe (`test_lint_policy.py`) and workflow hardening tests (`test_workflow_hardening.py`) | `scripts/` |
| CI, lint | `.github/workflows/ci.yml`, `.golangci.yml`, `.squawk.toml`, `Makefile` |
| PR review by Claude | `.github/workflows/claude.yml` |

### Stack

Versions are in `go.mod` (Go toolchain included), `.github/workflows/ci.yml` and `Makefile` (golangci-lint,
gorelease), `internal/pgtest.DefaultImage` (PostgreSQL), `go-migrate` (test-only, FIN-318). The same pieces as the services where needed: pgx v5,
golang-migrate, grpc-go, protobuf, prometheus client_golang, testcontainers-go, squawk. A new dependency is named in
the PR with why it is needed: every service that imports the library inherits it.

## Build in an agent container

- Go: the toolchain from `go.mod` is downloaded by `GOTOOLCHAIN=auto` (the default) on the first `go` command.
- `fin-assist/*` modules come from git, not the proxy: `GOPRIVATE=github.com/fin-assist/*` (set by `Makefile`; export
  it for plain `go` commands).
- Docker is usually absent in an agent container, so testcontainers cannot start PostgreSQL. Run a local server and
  point the tests at it: `TEST_DATABASE_URL='postgres://postgres@localhost:<port>/postgres?sslmode=disable' make test`.
  Without either, integration tests are skipped locally and fail in CI (`CI` set) — a skip is not a pass.
- `make lint` and `make api` run the pinned tools through `go run`; no separate install.
- The script tests need PyYAML (`python3-yaml`, or `pip install pyyaml`): `scripts/test_workflow_hardening.py` parses
  the workflows with `yaml.safe_load` and fails without it. `scripts/test_lint_policy.py` also builds the pinned
  golangci-lint and downloads the probe's pinned modules (prometheus, grpc, otel, x/sys; versions in the script or from
  `go.mod`), so it needs the module proxy.
- `make cover` needs the `covdata` tool, which a toolchain downloaded by `GOTOOLCHAIN` lacks ("no such tool
  covdata"): build it once with `T=$(go env GOTOOLDIR); chmod u+w "$T"; go build -o "$T/covdata" cmd/covdata`.
- Every test or lint run gets a timeout (`timeout 600 make test` or the tool's own): a hung run blocks the session.
- To try the library in a service before a release: `go mod edit -replace github.com/fin-assist/go-<name>=../go-<name>`
  in a local clone of the service. A `replace` never lands in a service PR.

## Principles

The same priorities and rules as the services (`AGENTS.md` of `fin-assist/go-service-template`, «Principles»):

1. **Test coverage** — a change without its tests (Tests below) is not done, however good the code.
2. **Clarity** — code a reader understands without the author: plain control flow, explicit names, no cleverness.
3. **Security** — the Security rules of the review are requirements, not trade-offs; this place in the order decides
   only between options that all meet them.
4. **Performance** — optimise when a measurement shows the need, not in advance; a faster but less clear version
   comes with the benchmark that justifies it.

- **No frameworks**, **standard library first**, **SOLID the Go way** — as in the services; `depguard` rejects the
  common frameworks, ORMs, DI containers, third-party loggers, testify and gomock.
- **A library is a dependency of five services.** Its exported API is a contract: small, documented, hard to misuse.
  Prefer a few functions and option structs over a wide surface; every exported identifier is a promise kept until
  the next major version (`docs/releasing.md`).

## Writing code

- Code, comments, identifiers, commit messages — English. `docs/*.md`, `README.md`, `CHANGELOG.md`, PR title and
  description — Russian. A doc comment on every exported identifier, saying what the caller must guarantee
  (inside a transaction, on the primary, after the identity interceptor) and which errors come back.
- **No process-wide state.** The service passes everything in: `*slog.Logger`, `prometheus.Registerer`, the clock
  (`func() time.Time`), settings in an options struct with documented defaults. No `init()` with side effects, no
  package-level mutable variables, no reading the environment; `forbidigo` rejects outside tests and `internal/pgtest`
  the default loggers (`fmt.Print*`, `print`, package-level `slog`/`log`, `log.Default`/`SetOutput`, the global
  `grpclog` functions), the default metrics registry (`prometheus.MustRegister`/`Register`/`Unregister`/
  `DefaultRegisterer`/`DefaultGatherer`, `promauto.New*`, `promhttp.Handler`), default HTTP, gRPC and OpenTelemetry
  globals (`http.DefaultClient`, `http.Get`, `http.Handle`, `http.Serve`/`ServeTLS`, `http.ProxyFromEnvironment`,
  `x/net/http/httpproxy.FromEnvironment`, the grpc-go `binarylog.SetSink`, `encoding/gzip.SetLevel` and registries `encoding.RegisterCodec`/`RegisterCodecV2`/`RegisterCompressor`, `resolver.Register`/
  `SetDefaultScheme`, `balancer.Register` and `grpc.EnableTracing`, `otel.Get*`/`Set*`/`Tracer`/`Meter`/`Handle`,
  `otel/log/global`), `os/signal` and the environment (`os.Getenv` and the rest of the family, `os.Args`,
  `os.User*Dir`, `syscall` and `golang.org/x/sys/unix|windows` env calls, Win32 included: `GetEnvironmentStrings`,
  `ExpandEnvironmentStrings`, `CreateEnvironmentBlock`, `Token.Environ`). By the owner's decision it
  also rejects package-level `flag` functions and `flag.CommandLine` (a passed `*flag.FlagSet` from `flag.NewFlagSet`
  stays allowed), `os.Stdout`/`os.Stderr`/`os.Stdin` (take an `io.Writer`/`io.Reader`), `runtime/debug.Set*`,
  `runtime.GOMAXPROCS`/`SetDefaultGOMAXPROCS` and changing the working directory (`os.Chdir`, `(*os.File).Chdir`,
  `syscall`/`x/sys` `Chdir`/`Fchdir`/`SetCurrentDirectory`), the root and the DLL search path (`Chroot`,
  `SetDllDirectory`/`SetDefaultDllDirectories`), the umask and resource limits (`Umask`, `Setrlimit`, `unix.Prlimit`)
  and the `syscall`/`x/sys` forms of the standard streams, temp directory and command line (`Stdin`/`Stdout`/`Stderr`,
  `GetStdHandle`/`SetStdHandle`, `GetTempPath`, `GetCommandLine`/`CommandLineToArgv`). A library takes a pool or worker count from the service's
  options; a read-only `runtime.GOMAXPROCS(0)` as the default of such an option carries `//nolint:forbidigo` with
  the reason «read-only». `os.TempDir`, `os.CreateTemp` and `os.MkdirTemp` are rejected entirely: with an empty `dir`
  they use the process-wide temp directory and lint cannot tell an empty argument from a passed one, so a library
  takes the directory from the service and creates files in it (`os.OpenFile`, `os.Mkdir`); a deliberate
  `CreateTemp(dir, …)` with a validated non-empty `dir` from the caller carries a `//nolint:forbidigo` with that
  reason. `depguard` rejects even a
  blank import of `expvar`, `net/http/pprof` and `golang.org/x/net/trace` (handlers on `http.DefaultServeMux` in
  `init`), `google.golang.org/grpc/grpclog/glogger` (replaces the gRPC logger in `init`), `github.com/golang/glog`,
  `k8s.io/klog`, `go.uber.org/automaxprocs` and `github.com/KimMachineGun/automemlimit` (runtime tuning in `init`),
  `google.golang.org/grpc/xds` (registers resolvers and balancers in `init`, reads the environment) and the whole
  `net/http/cgi` and `net/http/fcgi` (serve `http.DefaultServeMux` for a nil handler; cgi reads the environment). Injected forms stay allowed: `promauto.With(reg)`, `promhttp.HandlerFor`,
  `InstrumentMetricHandler(reg, …)`, `grpclog.LoggerV2` and `NewLoggerV2*` for an adapter the service installs,
  methods of a passed `*slog.Logger`, `Registerer`, `*http.Client`, `TracerProvider` or `MeterProvider`.
  `scripts/test_lint_policy.py` runs the pinned linter (version from `Makefile`, equal to `ci.yml`) on a hermetic probe
  module with pinned dependencies, for linux and windows: every forbidden and allowed form, chains such as
  `prometheus.DefaultRegisterer.MustRegister(c)` or `log.Default().Printf` and an aliased import are reported by their
  first identifier. The list is a best-effort denylist, not a proof: a newly found path to process-wide state is
  closed when it is found, in `go-library-template` first and then in the libraries.
  Lint cannot see options: a library always passes the providers explicitly (`otelhttp`/`otelgrpc` with
  `WithTracerProvider`/`WithMeterProvider`; without them they take the global ones), sets `Transport` on an
  `&http.Client{}` (an empty one uses `http.DefaultTransport` and so `ProxyFromEnvironment`) and `Handler` on an
  `&http.Server{}` (an empty one serves `http.DefaultServeMux`). grpc-go itself imports
  `golang.org/x/net/trace` unless built with the `grpcnotrace` tag; that is the service's build decision. The probe's
  manual pins (`MANUAL` in the script) are raised by hand: Renovate does not read them. A module that `go.mod`
  requires is pinned at the higher of `go.mod` and its manual pin and must stay exactly there after `go mod tidy`;
  a manual pin of a module absent from `go.mod` may be raised by tidy (a newer grpc or otel can require it), never
  lowered.
- **The service owns the transaction.** A library that writes together with the effect (idempotency, outbox,
  tombstone) takes it through a consumer interface — `interface{ Tx(context.Context) (pgx.Tx, bool) }`, implemented
  by `postgres.DB` of the service template — and returns an error when there is none, instead of writing outside it.
  It never commits or rolls back a transaction it did not open, and calls nothing over the network inside one.
- **Errors**: client-facing errors only through `finerr` constructors; `status.Error`, `status.New`,
  `errdetails.ErrorInfo` are forbidden by the linter; an internal refusal is a bare code built in one function with
  a nolint and its reason. Errors the service handles are exported sentinel values or types, checked with
  `errors.Is` / `errors.As`, and listed in the doc comment.
- **SQL construction:** write fixed-structure queries as plain SQL with parameters (`$1`, `$2`, … for
  PostgreSQL). Use [Squirrel](https://github.com/Masterminds/squirrel) (`github.com/Masterminds/squirrel`) only
  when assembling a query dynamically, such as optional predicates or varying column lists. Changing argument
  values alone does not make a query dynamic. This limited use is an approved exception to "standard library
  first". Parameterize values; keep identifiers and structural fragments constant or from an explicit allowlist.
  Use `PlaceholderFormat(sq.Dollar)` for PostgreSQL builders. If a dynamic query cannot be expressed by the
  builder, explain the limitation beside its manual construction and preserve parameterization.
- Database: SQL with parameters only (`$1`), table and column names are constants; tables are named
  `<library>_<name>` so they never collide with a service's. Migrations follow `docs/migrations.md` of the service
  template; a released migration never changes (`docs/releasing.md`, «Миграции»).
- Time: "now" comes from the injected clock, never `time.Now()` in logic or queries; durations for metrics and
  timeouts may use `time.Now()` / `time.Since`. Money: integer minor units (`int64`), never floats.
- Concurrency: an exported type says whether it is safe for concurrent use; a background goroutine is started only
  by a `Run(ctx)` the service calls (a `worker.Job`), stops when the context ends and never outlives it.
- Logs: `*Context` methods of the passed logger; no tokens, keys, DSNs, statement contents or assistant dialogue in
  logs or errors. Metrics: names `finassist_<library>_…`, help texts, bounded labels only — never `user_id`, an
  object ID or free text; the README lists them.
- Dev-only behaviour (a dev token source, a fake verifier) is a separate constructor the service can call only in
  `ENV=dev`, and the library documents that; it never switches on by itself.

## Tests

Every change comes with its automated tests in the same PR — not in a follow-up:

- **Unit tests — all new code.** Every new or changed function or method with logic has unit tests in its package:
  no database, no network, fakes of the interfaces the code declares; the main path and every branch and error the
  code handles.
- **Integration tests — all new functionality.** Every new or changed behaviour against its real dependencies:
  queries, migrations, transactions and locks against PostgreSQL (`pgtest.DSN`), not mocks of pgx; gRPC
  interceptors and clients through a real server on bufconn; background jobs through a real run. Every migration
  passes up → down → up (`migrations_test.go`).
- **Usage tests — the integration guide works.** The library is exercised the way a service uses it, by the steps
  of README «Внедрение»: its migrations applied by the service's rules, its constructor wired with a transaction
  manager like `postgres.DB`, its interceptors in a real gRPC server chain on bufconn, a call made with a generated
  client, and the status code, `ErrorInfo.reason` and metadata asserted against `docs/grpc-api.md`. An `Example…`
  function per main entry point shows the same code in the package docs.

Also:

- A bug fix comes with a test that fails without the fix and reproduces the reported case.
- Security-relevant behaviour (a forged or expired token, a foreign caller, a deleted user, a replayed request,
  hostile input) has negative tests: what must not happen is asserted — the handler did not run, no row was
  written, nothing leaked.
- Concurrent behaviour (two pods, a duplicate request in flight, a crash between steps) is tested for real: two
  connections, two goroutines released together, a transaction rolled back mid-way; no sleeps for synchronisation —
  wait on a channel or a condition with a deadline. Run with `-race`.
- Tests assert behaviour; a test that only executes lines or asserts on a fake it set up itself is not coverage.
- FIN-243 requires at least 80% of Go statements independently in unit, integration and consumer API suites.
  All three derive the same `-coverpkg` list from `go list -race ./...`, excluding only the exact package
  `<module>/internal/pgtest` by the owner's FIN-243 decision. Nested and similarly named packages remain included.
  Tests still run against `./...`, including the harness tests. An empty instrumentation list fails collection.
  No analyzer-only exclusions or other package/file exclusions are allowed.
  `go-test-coverage/v2` v2.20.1 enforces total80; its rounding accepts 79.95% as 80.0%.
  Combined coverage is an informational union, never a substitute for any individual gate.
- Name tests/fuzz targets `TestUnit...`/`FuzzUnit...`, `TestIntegration...`/`FuzzIntegration...` or
  `TestAPI...`/`FuzzAPI...`. Executable public Go examples belong to API. A race-enabled inventory runs before
  every collection and rejects unclassified tests. Existing compile-only examples remain compile-only.
- Unit tests have no database or network. Integration exercises real dependencies. API tests exercise public Go
  entry points as a service consumer; stateful API tests use real PostgreSQL in caller-owned transactions.
- Empty suites, skipped or failed actual tests, and missing/empty/header-only profiles fail. Missing PostgreSQL
  fails in CI. The small JSON/grep guard validates execution and profile inputs; it calculates no coverage.

## Checks

Run `make test` (or `make cover`), `make lint` and `make api` before pushing. The existing `go` CI job retains
module tidiness, vet, script and changelog checks, then independently collects and checks all three suites.
Each collection/check outcome must succeed. `api` remains the gorelease compatibility check, distinct from API tests.
Existing migration, lint, release and Renovate checks retain their purpose.

Workflows: every `actions/checkout` except `release.yml` and `claude.yml` sets `persist-credentials: false`, and a
`run:` script reads branch names and event fields from `env:`, never from `${{ … }}` in its text (FIN-301,
`scripts/test_workflow_hardening.py`, which checks the values decoded by PyYAML in `*.yml` and `*.yaml`).

### Coverage reports (FIN-243)

`make cover-unit`, `make cover-integration` and `make cover-api` collect fresh `*.out` and `*.jsonl` files and enforce
their respective threshold. `make cover` runs all three gates. `make cover-combined` uses the tool's built-in comma
profile union. CI saves profiles, execution JSON, native breakdowns and reports as artifacts and publishes a run
summary. There is no PR-writing token or custom coverage parser/calculator. Run script regressions with
`python3 -m unittest discover -s scripts -p 'test_*.py'`.

## Workflow

- Branches: `<kind>/<slug>` — `feature/…`, `fix/…`, `tooling/…`; a PR targets `main`. Renovate's `renovate/…`
  branches are the exception. Commits: imperative English subject (`Add …`, `Fix …`, `Address review: …`).
- PR title and description in Russian: what changed and why, the `CHANGELOG.md` line, whether the change is
  incompatible (`docs/releasing.md`) and what services must do, deviations from the contracts or docs, how it was
  checked. A PR for a single issue starts its title with the key (`FIN-109 Доставщик outbox`); a PR for several
  issues lists all of them in the description, each linked (`https://finassist.youtrack.cloud/issue/FIN-<n>`).
- A dependency on a contract change: the contract PR is merged and `gen/go` tagged first, then the library PR bumps
  the module.
- Releases follow `docs/releasing.md`: an agent may open the release PR on the owner's command; the owner merges it
  and pushes the tag. An agent never pushes, moves or deletes a tag.
- After the PR is open and all planned changes are pushed, request a review with one PR comment in Russian addressed
  to both reviewers; it starts with `@codex @claude`:

  ```
  @codex @claude проведите ревью этого PR по Code Review Rules в AGENTS.md, включая разделы Security, Tests и
  Compatibility. Замечания — на русском, inline на изменённых строках. Не придирайтесь к формулировкам документации и
  комментариев: замечание по тексту только если он искажает поведение кода. Особое внимание: <что именно и почему>.
  ```

  - `@claude` — `.github/workflows/claude.yml` (runs from the default branch), Claude Opus 5.5
    (`claude-opus-5-5`, `--effort high`; FIN-204). Needs the Claude GitHub App and the `CLAUDE_CODE_OAUTH_TOKEN` secret.
  - `@codex` — the Codex GitHub integration under the owner's ChatGPT subscription. Its model is chosen by OpenAI
    and cannot be pinned from here; a pinned model would need the OpenAI API with per-token billing, which was
    declined (FIN-103).
  - Claude review is requested once `.github/workflows/claude.yml` is on `main`; Codex is requested independently when its integration is available. Comment-triggered Claude workflows run
    from the default branch.
  - Replies in threads and other comments never mention `@codex` or `@claude`: any mention with text starts them
    again.
- Waiting for the review: react to the PR's GitHub events when the session receives them; otherwise check 3 minutes
  after the request, then every 2 minutes. A reviewer has answered when it posted its review or summary for the
  latest commit; for Codex (`chatgpt-codex-connector[bot]`) «Didn't find any major issues» or a 👍 without a comment
  also counts. A reviewer silent for 15 minutes after the last request: say so in the PR and to the owner, stop
  waiting for it. The cycle ends when both answered the latest request and no thread is open.
- Address findings in follow-up commits on the same branch, reply in the thread and resolve it, then request a
  review again. Agents never merge a PR; the owner does.

## Code Review Rules

Read by `@claude` (through `CLAUDE.md`) and by the Codex integration, which looks for this heading. Three areas get a
dedicated pass on every PR: Security, Tests and Compatibility. A flaw here is copied into every service that
updates the library.

### General

- Write all review comments in Russian (summary and inline). Identifiers and quotes stay as they are.
- Report only verified problems with a concrete failure scenario: which service, caller, request or data, what goes
  wrong. Skip style nits, naming preferences and wording of comments and docs unless the text misstates the
  behaviour.
- Weigh findings by the priorities of «Principles»: missing tests first, then clarity, then security beyond the
  mandatory rules, then performance; a performance finding needs a measurement or an evident cost, not a guess.
- Post each finding as an inline comment on the changed line. The summary lists what was checked, with a
  «Безопасность», a «Тесты» and a «Совместимость» part; «not affected» is a valid answer, with one line why.
- Check that the PR references its YouTrack issue, adds its `CHANGELOG.md` line including docs/test/CI-only changes
  (except bot-only dependency PRs of Renovate, FIN-211). A release PR checks the whole `git diff <last tag> -- go.mod`:
  every raised `go` directive or major of a shared dependency has a «Несовместимо» line, from its PR or added by the
  release PR; undescribed Renovate changes (`git log -p <last tag>..HEAD -- go.mod`) go under «Изменено». Also check
  that the PR updates `README.md` («Внедрение»), this file and `docs/` when it changes what they describe.

### Security

The services handle a user's bank operations, phone number, statement files and assistant dialogue (personal data
under 152-ФЗ); a library sits on their trust boundaries. A finding names what leaks or breaks and how a client,
another user or a compromised service gets to it. Check:

- Identity: a user comes only from a verified token (`sub`), a caller only from a verified service token; signature,
  algorithm (no `none`, no switch to a symmetric one), `exp`, `iss`, `aud` are checked; unknown key IDs refresh keys
  with a rate limit; a missing rule denies.
- Object access: every query by a key is scoped by its owner (`user_id`, scope, endpoint); another user's record is
  never returned or overwritten.
- Errors: client-facing errors only through `finerr`; error texts, details and logs contain no SQL, stack traces,
  tokens, keys, DSNs or other users' data; `PERMISSION_DENIED` to a foreign caller carries no details.
- Secrets: tokens, keys and DSNs never in logs, metric labels or error texts; stored secrets (an idempotent response
  of a token refresh) are encrypted and kept no longer than the contract says.
- SQL: parameters only; nothing from a caller selects a table, column, `ORDER BY` fragment or file path.
- Transactions: the record and the effect are written in the service's transaction; no write outside it, no network
  call inside it; a deleted user's data is not written back.
- Untrusted input: sizes, counts, lengths and nesting are bounded; a hostile value ends in a handled error, not a
  panic, a hang or memory growth.
- Process-wide state: no global logger, registry, environment or mutable package variable that one service's
  setting could leak into another code path.
- Dev hooks: a dev-only constructor cannot be reached by configuration in `stage` or `prod`.
- New dependencies: flag a new library that gets network, storage or data access, so the owner decides on it.

### Tests

The criterion is not a percentage of lines but whether the use cases of the changed functionality are covered; the
80 % target is the author's minimum. For each behaviour the PR adds or changes:

1. List its use cases from the code, the contract and the issue: the main path; alternative paths; every error a
   service can get back (missing transaction, conflict, timeout, a database error, a cancelled context); boundary
   values; repeated and concurrent calls (a retry with the same key, two pods at once, a crash between steps); a
   deleted user; a newer or older peer.
2. Map each case to the test that covers it and check the three levels of Tests above: new code without a unit test,
   new functionality without an integration test, or an entry point without a usage test is a finding.
3. Post a finding for an uncovered case only when it is reachable in the changed code and its behaviour is added or
   changed by the PR; name the concrete case and the layer that should cover it.

Also check that a test would fail if its case broke, that every migration passes up → down → up, and that
security-relevant behaviour has negative tests.

### Compatibility

Every service pins a version and updates through a Renovate PR (`docs/releasing.md`). For each changed exported
identifier, behaviour, migration and dependency check:

- Whether it is incompatible by «Что считается несовместимым» in `docs/releasing.md` (the `api` job's gorelease
  report helps for the Go API, not for behaviour). An incompatible change says so in the PR and in `CHANGELOG.md`
  under «Несовместимо» with what a service must change; from `v1` on it needs a new major path instead.
- A released migration file is never edited; a schema change a service depends on goes in two releases.
- Defaults, error codes, metric names and labels keep their meaning; a new option defaults to the old behaviour.
- A new exported identifier is needed by a service now, not "for later": the surface only grows.
- The minimum Go version and the `contracts/gen/go` version are not raised without a reason in the PR.

### Checklist

- New code has unit tests; new functionality has integration tests; entry points have usage tests and examples.
- Every PR has a `CHANGELOG.md` line, except bot-only Renovate dependency PRs (summarised by the release PR);
  incompatible — under «Несовместимо», and the PR says so.
- A new client-facing error goes through `finerr`; a new reason or metadata key exists in `contracts` first.
- A new query by a key is scoped by its owner and has a test with another owner's key.
- A write that goes with an effect uses the service's transaction and fails without one.
- A new migration follows the service template's `docs/migrations.md`, passes squawk and up → down → up, and no
  existing file changed.
- A new metric is `finassist_<library>_…` with bounded labels and is listed in the README.
- No logger, registry, clock or setting is taken from process-wide state.
- Each suite independently meets total80; any deficit is reported explicitly with reachable scenario gaps.

## Renovate automation

FIN-211: dependency-only Renovate PRs may be merged/closed by the reviewed `renovate-policy.yml` controller, after exact-head required CI and bot-only commit checks. Ordinary PRs and release-to-main PRs still require the owner. Do not enable native automerge or bypass protections. Preserve base policy in `renovate-base.json` (schedules, groups, allowed versions and manual Go upgrades); generated target/version approvals live on the separate `renovate-policy` branch. Failed exact values are isolated for explicit Dashboard approval; new values keep their original groups. AndroidX/Compose and mixed groups never auto-merge. See `docs/renovate-policy.md` for credentials, safeguards, bootstrap and verification.
