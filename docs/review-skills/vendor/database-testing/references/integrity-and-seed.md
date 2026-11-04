# Integrity and fixture checks

For Plainfin, apply [the shared project policy](../../qa-project-context/references/plainfin-policy.md)
and the schema/service contracts. Use the actual Go/pgx harness and a real PostgreSQL
instance for database behavior. No new ORM, assertion framework or seed CLI is needed.

## Test the enforcing layer

Test changed declared NOT NULL, DEFAULT, primary/unique keys and permitted foreign
keys. Set up otherwise-valid rows so each negative case reaches the intended
constraint instead of failing an earlier cast or unrelated constraint. Assert
PostgreSQL SQLSTATE and the relevant constraint identity where available.

For an existing pgx-based Go test, the structured-error assertion has this shape
(the error comes from the real operation under test, not a fake):

```go
var pgErr *pgconn.PgError
if !errors.As(err, &pgErr) {
    t.Fatalf("expected PostgreSQL error, got %v", err)
}
if pgErr.Code != expectedCode {
    t.Fatalf("SQLSTATE = %s, want %s", pgErr.Code, expectedCode)
}
if expectedConstraint != "" && pgErr.ConstraintName != expectedConstraint {
    t.Fatalf("constraint = %q, want %q", pgErr.ConstraintName, expectedConstraint)
}
```

Use the repository's existing `errors` and `github.com/jackc/pgx/v5/pgconn` imports.
`23502`, `23505` and `23503` identify not-null, unique and foreign-key violations.
Do not depend on English error strings. The expected code/name comes independently
from the required schema/contract, not production constants being tested.

Plainfin value/state validation belongs in the service. Do not introduce CHECK,
EXCLUDE, triggers or stored functions to reject business values. Assert those
refusals at the service/API layer, including that rejected writes leave state
unchanged. Not every table needs all constraints; respect library-specific foreign
key restrictions and actual deletion semantics. CASCADE is tested only when declared.

## Query and data semantics

For relevant queries, use at least two owners and assert both allowed access and
absence of cross-user rows. Include NULL/empty results, duplicates, exact page
boundaries, stable tie-breaking order and repeated/concurrent actions where the
contract makes them meaningful. Exercise actual SQL, not an in-memory substitute.

Amounts remain integer minor units. Include exact round trips and applicable signed,
zero and range boundaries without float conversion. Respect agreed integer widths,
currency and timestamp/time-zone semantics; an ISO string alone does not prove the
service's time-zone behavior.

Anti-joins, NULL counts and distinct counts can investigate an agreed invariant.
A nonzero count is a defect only if the invariant requires otherwise: nullable
columns, per-user uniqueness, historical/tombstone rows and intentionally absent
cross-service FKs must not become generic migration demands.

## Reproducible fixtures

Use synthetic values, a controlled clock and stable test-local identifiers. Scope
IDs to each isolated database/schema or test fixture to avoid collisions in parallel
runs. A deterministic sequence within one fixture is sufficient; do not replace all
existing fixture conventions with factories, globally fixed UUIDs or faker.

Seed only the rows needed for the scenario. If the existing harness defines larger
volume profiles or idempotent seed routines, test their documented behavior; named
`test/staging/demo` profiles and upsert everywhere are not universal requirements.
Do not use production PII or copy a production database to obtain realistic data.

## Isolation and cleanup

Use existing per-test databases/schemas or guaranteed cleanup. A rollback wrapper
isolates only writes through that same transaction/connection. Committed work,
background workers, migrations or separate connection pools escape it; use distinct
state for those scenarios. Savepoints do not isolate concurrent users of one shared
connection. Give parallel tests their own state/connection ownership.

A PostgreSQL constraint error aborts its transaction until rollback. Use a fresh
transaction/case or appropriate harness savepoint handling before the next assertion.
Register cleanup early, including failure paths, release connections, stop workers
and close owned pools even when setup partially fails. Do not rely on test ordering.
