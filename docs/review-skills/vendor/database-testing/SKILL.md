---
name: database-testing
description: >-
  Validate database integrity, migration upgrades and applicable recovery paths, verify schema
  constraints, manage seed data, detect migration drift, and identify query performance
  issues. Covers PostgreSQL, MySQL, MongoDB with Prisma, TypeORM, Drizzle, and SQLAlchemy,
  plus Testcontainers test databases. Go/PostgreSQL work follows its existing real-database harness.
  Use when: "database test," "migration test," "migration rollback," "rollback test,"
  "data integrity," "SQL test," "schema validation," "seed data," "query performance,"
  "Testcontainers."
  Not for: synthetic data generation/masking at scale — use test-data-management;
  Docker/IaC test-environment provisioning — use test-environments; SQL injection — use security-testing.
  Related: test-data-management, test-environments, security-testing, ci-cd-integration.
license: MIT
metadata:
  source: https://github.com/petrkindlmann/qa-skills
  upstream_commit: "62434253c2592f27b494609c27f224939c86346d"
  adaptation: "Plainfin: existing Go harness, permitted constraints, recovery-aware migrations and contextual plans"
  author: kindlmann
  version: "2.0"
  category: specialized
---

## Plainfin integration

For FinAssist / Plainfin tasks, read [the shared project policy](../qa-project-context/references/plainfin-policy.md)
before this workflow. Canonical project/repository rules govern generic
templates, context files, tool selection, test runs and completion criteria. The shared policy also overrides generic
Verification, Done When, reporting and tool requirements in this vendored copy.

Use existing Go `testing`, pgx, real PostgreSQL and the repository migration harness
(`go-migrate/migratetest` where already used); inspect its real API instead of
inventing helper signatures. Preserve PostgreSQL 18 image/tool pins and independent
coverage gates. Generic ORM/npm/Jest recipes below apply only to projects already
using that stack; do not introduce them into Go to follow this skill.

Plainfin keeps value/state validation in service code. Do not require CHECK,
EXCLUDE, triggers or stored functions; verify declared NOT NULL, DEFAULT, PK/UNIQUE
and permitted FK behavior only where applicable. Preserve repository/library FK
restrictions. Money remains integer minor units (`int64`, `*_minor`, agreed integer
types), not floats or a new NUMERIC representation. Examples do not widen IDs.
An optional `.agents/qa-project-context.md` is an index, not a prerequisite.


<objective>
A migration command can succeed while losing data, and a query-plan assertion can pass while the workload becomes expensive. This skill checks actual upgrades and applicable recovery paths, declared integrity constraints, deterministic state, schema reproduction and meaningful query-performance evidence. Follow repository contracts rather than imposing every generic recipe.

**Before starting:** check `.agents/qa-project-context.md` for database type, ORM, migration tooling, and environment config — they shape every pattern below.
</objective>

---

## Discovery Questions

Check `.agents/qa-project-context.md` first — if it exists, use it and skip anything already answered there. Then:

1. **Database type:** PostgreSQL, MySQL, SQLite, MongoDB, or multi-database? Each has different constraint syntax, migration tools, and performance profiling.
2. **ORM / query builder:** Prisma, TypeORM, Drizzle, Sequelize, SQLAlchemy, Django ORM, or raw SQL? The ORM determines migration tooling and test patterns.
3. **Migration tool:** Prisma Migrate, TypeORM migrations, Flyway, Liquibase, Alembic, knex, or custom? This determines how to test forward and backward migrations.
4. **Test database strategy:** isolated DB per test, transaction rollback, Testcontainers, or shared DB with cleanup? Affects speed and reliability.
5. **Existing seed data:** factories, fixtures, or seed scripts? Check `prisma/seed.ts`, `seeds/`, `fixtures/`, or factory patterns.
6. **Performance baselines:** any existing query benchmarks or slow-query monitoring?

---

## Core Principles

1. **Test real upgrades and applicable recovery.** For reversible changes, exercise up → down → up through the actual runner in an isolated database, checking promised data preservation at each step. For an intentionally irreversible change, verify the agreed forward-fix/backup-restore or staged recovery strategy; do not invent unsafe down SQL or claim an unexecuted recovery passed.

2. **Test the enforcing layer.** Verify the changed declared constraints and intended defaults on a real database. In Plainfin, business-value/state validation stays in service/API tests; not every table needs every constraint. Assert SQLSTATE and relevant constraint identity, not localized PostgreSQL error messages.

3. **Reproducible seed data.** Use synthetic data, controlled clocks and identifiers unique within isolated test state. Follow existing fixture conventions; fixed global IDs in a shared database can collide. Random data is suitable only with reproducible input and meaningful assertions.

4. **Isolate database state per test.** Tests that share state are order-dependent and flaky. Use transaction rollback, per-test databases, or guaranteed cleanup.

5. **Test the migration, not the ORM's sync.** `prisma db push` / `typeorm synchronize: true` skip the migration path your users will actually run. Always exercise the real migration files.

6. **Measure the required workload.** Ground regression checks in representative data, meaningful parameters and an agreed budget or comparable baseline. A Seq Scan is not itself a defect and an Index Scan is not proof of efficiency. A focused disposable sensitivity experiment can help; dropping an index is not a mandatory oracle.

---

## Migration Testing

For runnable migration test code, see `references/migration-tests.md`.

### Forward Migration Validation

Provision a harness-owned isolated database, run the actual committed migration sources through the existing runner, then verify intended types, nullability, defaults, keys and data through catalog and behavior assertions. Test both fresh installation and supported upgrades when changed. Do not reset a database inferred from an environment variable or unrelated local default.

### Rollback Testing

Prisma has **no** `migrate down` / `migrate rollback` command. `prisma migrate resolve --rolled-back` is **not** a rollback tool — it only fixes a migration whose `migrate deploy` *failed*, and it throws on a cleanly-applied one. The supported test for a reversible change: apply forward, capture state, run the hand-written `down.sql` directly (`psql -f down.sql`), assert the reverted object is gone, then re-apply. For a reversible migration in a project using this mechanism, maintain the appropriate inverse SQL. Do not require a down file for intentionally irreversible changes or replace the Go migration runner with Prisma commands.

For **TypeORM and Sequelize**, both ship native revert commands (`dataSource.undoLastMigration()`, `sequelize-cli db:migrate:undo`); swap them in for the `psql -f down.sql` step — the capture → revert → assert → re-apply shape is identical.

For other tools, use the installed version's actual migration/recovery API and the repository's declared reversibility policy; do not infer support from a generic example.

### Data Preservation During Migration

To test that an added column preserves existing rows, apply migrations up to N-1, insert data, then apply the migration under test and assert the rows survived (new nullable column carries its default or null). **`prisma migrate deploy` has no `--to` flag** — it applies *all* pending migrations. To stop at N-1, deploy a migrations directory containing only migrations up to N-1 (stage it in CI), then deploy the full directory. Tools with real targeting (Flyway `-target=`, Alembic `upgrade <rev>`) use the native flag instead.

### Migration Drift Detection

The most common real migration bug: someone edits the DB or the schema without a matching migration, so the committed migrations no longer reproduce `schema.prisma`. Use the existing schema reproduction/catalog checks; a Prisma project may use its installed `migrate diff` API, but this does not require Prisma or a new CI gate in Go. See `references/migration-tests.md`.

### Schema Snapshot Comparison

Capture a `pg_dump --schema-only` snapshot before and after the migration and diff table-by-table so only the intended tables changed. See `references/migration-tests.md`.

### Other ORMs

**TypeORM:** `DataSource` with `migrationsRun: false`, then `dataSource.runMigrations()` and `dataSource.undoLastMigration()` in tests. Same shape: apply all, verify schema, revert last, verify rollback.

**Alembic (Python):** test `alembic upgrade head` from empty DB, `alembic downgrade base` for full rollback, and an upgrade→downgrade→upgrade cycle to verify schema consistency. Use a fresh test database via fixture.

---

## Data Integrity Testing

For runnable constraint and referential-integrity code, see `references/integrity-and-seed.md`.

### Constraint Testing

Assert each changed declared constraint at the database layer: required values, uniqueness and permitted referential rules. Test CASCADE only where actually declared. Use SQLSTATE and constraint identity. Set up otherwise-valid rows so the intended constraint is reached, rather than an earlier cast or unrelated error. Business value/state rejection belongs in service tests, including unchanged state after refusal.

### Referential Integrity & Data-Quality Audit

Use anti-joins, distinct/NULL counts and ownership checks only against agreed invariants. Nullable columns, per-user uniqueness and historical/tombstone records can make nonzero counts legitimate; do not infer missing constraints from counts alone. Test cross-user read/write isolation explicitly where applicable. See `references/integrity-and-seed.md`.

### Data Type Validation

Test exact integer minor-unit round trips and relevant signed/zero/range boundaries without float conversion; preserve agreed integer widths. Test declared string limits and timestamp/time-zone behavior against the service contract, not only a formatted ISO string.

---

## Seed Data Management

For runnable factory, seed-script, and isolation code, see `references/integrity-and-seed.md`.

### Factory Pattern (TypeScript)

Build records from a `buildUser(overrides)` factory that increments a counter for stable, deterministic IDs and emails and uses a fixed timestamp (`new Date('2026-01-01T00:00:00Z')`, never `new Date()` with no argument), with a `createUser(pool, overrides)` helper that inserts and returns the record. See `references/integrity-and-seed.md`.

### Prisma Seed Script

Use `upsert` with fixed IDs so the seed is idempotent and re-runnable, and switch profiles on `process.env.SEED_ENV`: `test` (minimal, 2–3 users), `staging` (realistic volume, 50+ users), `demo` (curated). `staging` and `demo` extend `test`. See `references/integrity-and-seed.md`.

### Test Isolation with Transaction Rollback

A rollback wrapper isolates only writes using that same transaction/connection. Concurrent commits, separate pools, background jobs and migration transactions need harness-owned isolated databases/schemas or explicit cleanup. Savepoints do not isolate concurrent users of a shared connection. Register cleanup early, release connections on failure and do not rely on test ordering. See `references/integrity-and-seed.md`.

---

## Query Performance Testing

For runnable EXPLAIN ANALYZE and index-validation code, see `references/performance-and-docker.md`.

### Plan and workload evidence

Start with plain EXPLAIN where sufficient. Use representative volume, distribution,
selectivity and parameters with relevant statistics, and inspect the full plan tree,
not just its root. Assess rows/loops, reads, join/filter/sort work, spills and partition
pruning where relevant. Preserve NULL/duplicate behavior, ordering, user isolation
and exact money values before interpreting a performance change.

Seq Scan can be optimal for small tables or broad reads; an Index Scan can still do
excessive work. A root Limit, Sort, Aggregate or join may have an index below it.
Do not assert root `Node Type` matches `/Index/`, ban Seq Scan or force planner
settings to make the test pass. Use an agreed workload/budget or comparable baseline,
not universal 10/100 ms or 10K-row gates. Account for statistics, cache and runner variance.

EXPLAIN (ANALYZE, BUFFERS) executes the statement, including side effects. Use it
only within authorized scope in an owned disposable or explicitly permitted
environment, with statement/context deadlines. Rollback is not a general guarantee
against external side effects. No production SQL or destructive experiment is
required by ordinary PR review.

### Index validation

If an index is an intended schema contract, check its actual method, column/expression
order, predicate, uniqueness and validity as applicable. Do not infer sufficient
indexing from a column name substring or add an index per queried column. Evaluate
actual access paths and write cost. A focused regression experiment is optional;
dropping an index can leave a valid alternate plan and need not make a test fail.

## Docker-Based Test Database

Use the existing real-database harness: Go `testing`, pgx and `testcontainers-go`
where already declared, or the documented permitted test-server option. Preserve
current PostgreSQL 18 image/digest/default-image pins rather than moving tags or
patch versions advertised in an example. Read actual helper APIs before using them.

Keep container random ports distinct from the fixed-port model of an existing Compose
setup. Never default to resetting an unrelated local database. Ensure owned resources
are cleaned up on partial setup failure as well as success. No new container framework,
ORM, npm scripts or CI job is required merely by loading this skill. Database absence
is skipped/blocked according to repository policy, never a passed integration suite.

---

## Anti-Patterns

### 1. Testing against production database copies
Production data contains PII, is non-deterministic, and changes unpredictably. Use factories and seed scripts with synthetic data.

### 2. Shared database state between tests
Test A inserts a user; Test B assumes it exists; CI reorders them; Test B fails. Use transaction rollback or per-test cleanup.

### 3. Ignoring rollback testing
Test the actual recovery guarantees: up/down/up for reversible changes, or the agreed preservation and forward-fix/restore strategy for intentionally irreversible ones. A plan that has not run is not a tested recovery.

### 4. Faking rollback with `migrate resolve --rolled-back`
That command only repairs a *failed* migration and throws on a clean one. It does not revert schema. Revert with the real mechanism: `down.sql` for Prisma, `undoLastMigration()` for TypeORM.

### 5. Using ORM sync instead of migrations
`prisma db push`, `typeorm synchronize: true`, Django `migrate --run-syncdb` skip the real migration path. Tests must use the same mechanism as production.

### 6. Testing only happy-path queries
A query that returns rows when data exists is the easy case. Test empty result sets, nulls in optional columns, max result sizes, and queries against the wrong data.

### 7. Performance assertions that can never fail
A plan-node label or one noisy timing does not establish a workload guarantee. Tie assertions to representative data and an agreed budget/baseline. Do not require failure solely because an index was removed.

### 8. Seeding with random data
`faker.random()` without a fixed seed, `uuid()`, and `now()` produce different data every run, making tests non-deterministic. Use fixed seeds and fixed values: `faker.seed(42)`, explicit IDs, `new Date('2026-01-01T00:00:00Z')`.

---

## Verification

Use the smallest relevant existing test command with a real process deadline and
harness-owned isolated database. Inspect current CI artifacts when runtime is
unavailable; distinguish passed, failed, skipped, blocked and unrun checks.

Verify changed invariants and genuine migration transitions, including data
preservation and applicable recovery. Performance evidence needs the actual query,
parameters, data/configuration and a meaningful oracle; without measurement, report
a plan/risk assessment rather than a measured improvement. An optional focused
sensitivity experiment stays in a disposable copy and within task authorization.
Do not require a DROP INDEX mutation, three reruns or an invented command.

## Done When

- Changed database behavior has meaningful tests through the repository's actual
  harness; tests reach the intended constraints and verify service-owned rules at
  the service/API layer.
- Relevant migration paths check schema and data preservation; reversible recovery
  or an agreed irreversible-change strategy is covered to the extent actually run.
- Fixtures and cleanup isolate cases, users and parallel runs.
- Performance claims, if made, have representative workload evidence and preserve
  semantics. Seq Scan alone is neither failure nor evidence of regression.
- Actual results and remaining runtime limits are stated under repository review
  rules. No universal ORM/seed profile/report/new CI gate is required for every PR.

## Reference Files (in `references/`)

- **migration-tests.md** — Real runner, fresh/upgrade paths, applicable recovery, data preservation and schema reproduction.
- **integrity-and-seed.md** — Declared constraints, SQLSTATE, ownership/monetary checks, reproducible fixtures and isolation.
- **performance-and-docker.md** — Contextual EXPLAIN evidence, workload/index checks and existing PostgreSQL provisioning.

## Related Skills

- **test-data-management** — Synthetic data generation and masking *at scale* for non-production environments. Come here for in-test factories and seed scripts; go there for large realistic datasets and PII masking.
- **test-environments** — Docker/IaC provisioning of test databases, environment parity. This skill uses Testcontainers inside a test suite; test-environments owns the standing infrastructure.
- **security-testing** — SQL injection, database-level access control, and encryption verification. This skill tests integrity and correctness, not adversarial input.
- **ci-cd-integration** — Running migration and DB test jobs in CI pipelines and provisioning test databases in GitHub Actions.
- **performance-testing** — Load testing DB performance, connection-pool sizing, and query optimization under concurrent load (out of scope here).
