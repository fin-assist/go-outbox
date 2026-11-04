---
name: postgres-database-migration
description: "Plan and review PostgreSQL schema migrations, lock budgets, concurrent indexes, expand/contract changes, bounded backfills, validation and rollback. PostgreSQL 18 examples; execution stays within the task’s authorized scope."
license: Apache-2.0
metadata:
  author: tigerdata
  local_adaptation: plainfin-2026-11-04
---

# PostgreSQL Database Migrations

## Plainfin scope

For Plainfin, follow its current project process and repository AGENTS.md. Do not add CHECK, EXCLUDE, triggers or stored functions; value/state validation belongs in the service. Native NOT NULL, keys and FKs remain available. Use NUMERIC or the approved integer-minor-unit representation for exact money, not floating-point telemetry examples. Installing this skill does not select an extension or authorize a database mutation. Existing task authorization persists; resolve only genuinely missing decisions.

Compatibility: SQL examples target PostgreSQL 18. Use version-specific alternatives on older majors, particularly for native NOT NULL NOT VALID.

Plan schema changes against the actual PostgreSQL major, schema, data volume, writers, dependencies, and migration runner. Examples below target PostgreSQL 18 and `public.orders`; adapt object names safely. Execution follows the task's existing authorization. A request to assess a migration does not authorize applying it.

## Plan before DDL

- Identify application readers/writers, incoming and outgoing FKs, indexes, defaults, generated columns, views, permissions, and rollback requirements. Distinguish column/table identity from their names.
- Determine whether the runner wraps the file in a transaction. Concurrent index operations require their own transactions. Check available disk, replication/WAL budget and a realistic time budget.
- Test on an isolated representative copy, then test concurrent writers and lock contention where relevant. A database fork alone does not test production concurrency.
- Match project rules. For Plainfin: no new CHECK, EXCLUDE, triggers, or stored functions; validate value/state rules in the service. Native NOT NULL, UNIQUE/PK and FK remain available. Prefer application-coordinated migrations.

## Locks and defaults

Metadata-only does not mean non-blocking. Locks are held until the transaction ends, even if the DDL itself finishes quickly.

| Operation | Main table lock / caveat |
|---|---|
| ADD/DROP/RENAME COLUMN; SET DEFAULT | ACCESS EXCLUSIVE, normally brief after acquisition |
| ADD COLUMN with non-volatile DEFAULT | ACCESS EXCLUSIVE; PG11+ normally avoids rewriting rows |
| ADD CHECK or NOT NULL NOT VALID | ACCESS EXCLUSIVE; skips initial validation scan |
| ADD FOREIGN KEY, including NOT VALID | SHARE ROW EXCLUSIVE on referencing and referenced tables |
| VALIDATE CHECK/FK/NOT NULL | SHARE UPDATE EXCLUSIVE on altered table; FK also needs ROW SHARE on referenced table |
| CREATE INDEX | SHARE; blocks writes during build |
| CREATE INDEX CONCURRENTLY | Multiple phases/transactions; allows normal DML, can wait, fail or leave an INVALID index |
| ALTER TYPE | Usually ACCESS EXCLUSIVE; some binary-compatible changes avoid a rewrite |
| VACUUM FULL / CLUSTER | ACCESS EXCLUSIVE plus a rewrite |

`now()`/`CURRENT_TIMESTAMP` are STABLE. A default such as `now()` does not itself force a table rewrite on PG18. `clock_timestamp()` and `gen_random_uuid()` are volatile. Choose the historical meaning of a default separately: a migration timestamp is not necessarily the creation time of existing records.

```sql
BEGIN;
SET LOCAL lock_timeout = '3s';
SET LOCAL statement_timeout = '30s';
ALTER TABLE public.orders ADD COLUMN priority integer NOT NULL DEFAULT 0;
COMMIT;
```

Source: [ALTER TABLE](https://www.postgresql.org/docs/18/sql-altertable.html), [volatility](https://www.postgresql.org/docs/18/xfunc-volatility.html), [locks](https://www.postgresql.org/docs/18/explicit-locking.html).

## Add NOT NULL in PostgreSQL 18

Use a native not-null constraint, with separate transactions for addition and validation. Repair pre-existing NULLs according to domain rules before validation; do not invent replacement values.

```sql
BEGIN;
SET LOCAL lock_timeout = '3s';
SET LOCAL statement_timeout = '30s';
ALTER TABLE public.orders ADD CONSTRAINT orders_status_nn NOT NULL order_status NOT VALID;
COMMIT;

BEGIN;
SET LOCAL lock_timeout = '3s';
SET LOCAL statement_timeout = '10min'; -- choose from representative timing
ALTER TABLE public.orders VALIDATE CONSTRAINT orders_status_nn;
COMMIT;
```

On older PostgreSQL versions, a validated `CHECK (col IS NOT NULL)` can avoid a subsequent SET NOT NULL scan, but this is not available in projects prohibiting CHECK. Use a measured direct SET NOT NULL with a maintenance/locking plan instead of silently violating that rule. PG18's native syntax must not be sent to older servers.

## Add a foreign key or unique constraint

Adding NOT VALID still blocks writes briefly; validate separately. Index referencing columns when their access/deletion patterns justify it.

```sql
ALTER TABLE public.orders ADD CONSTRAINT orders_user_fk
  FOREIGN KEY (user_id) REFERENCES public.users(id) NOT VALID;
ALTER TABLE public.orders VALIDATE CONSTRAINT orders_user_fk;
```

For uniqueness, create a unique index concurrently outside a transaction block, check its validity, then attach it in a short, bounded transaction. Attaching does not make arbitrary expression or partial indexes into constraints. `USING INDEX` has additional restrictions, including partitioned tables.

```sql
CREATE UNIQUE INDEX CONCURRENTLY orders_tracking_uniq_idx ON public.orders (tracking_number);
SELECT indexrelid::regclass, indisvalid, indisready
FROM pg_catalog.pg_index
WHERE indexrelid = 'public.orders_tracking_uniq_idx'::regclass;
-- Attach only after successful build and validity check:
BEGIN;
SET LOCAL lock_timeout = '3s';
SET LOCAL statement_timeout = '30s';
ALTER TABLE public.orders ADD CONSTRAINT orders_tracking_uniq
  UNIQUE USING INDEX orders_tracking_uniq_idx;
COMMIT;
```

A failed concurrent build may leave an INVALID index and even continue enforcing uniqueness. Diagnose, then drop/rebuild only the intended failed object using an authorized recovery step. A timeout is not proof that no catalog object remains. Source: [CREATE INDEX](https://www.postgresql.org/docs/18/sql-createindex.html).

## Change types / rename or replace columns

First check whether a direct cast is binary-compatible and whether the required lock/rewrite is acceptable. For a large or incompatible change, use an application-coordinated expand/contract plan:

1. Add the target column with the correct target type; list the old column's dependencies, defaults and constraints.
2. Deploy writers that keep both representations consistent atomically in the same transaction. Drain old writer versions before starting the backfill. Alternatively pause writes for the complete copy and switch.
3. [Backfill](references/backfill-strategies.md) bounded portions, reading the source value while its row is locked. Handle NULLs, cast failures, precision changes and checkpointing explicitly.
4. Reconcile rows and restore the target's needed constraints/indexes/defaults. Validate under the selected concurrency strategy.
5. Deploy readers of the new representation; keep compatibility and a rollback window. Do not drop the source while old writers/readers or dependencies need it.
6. Perform any final name change and dependent-object changes in the planned short transaction. Only later remove the old representation under the authorized cleanup scope.

A naked backfill → DROP old → RENAME new is unsafe with concurrent writers. Renaming an object preserves its identity: FK/view dependencies do not transfer to a different column/table just because it takes the old name. Dropping the old PK can fail while FKs depend on it; replacing a PK also requires handling those FKs and the new key's NOT NULL/uniqueness before the switch. Recreate only approved dependencies; do not use CASCADE as an automatic workaround.

Source: [dependency tracking](https://www.postgresql.org/docs/18/ddl-depend.html).

## Timeouts, transactions and retries

`statement_timeout` includes planning/execution and lock waits from command arrival; `lock_timeout` limits each lock acquisition wait. Give long builds a measured budget and monitoring rather than treating a short default timeout as universal. Local settings inside a transaction avoid leaking session settings through a pool. Concurrent commands run separately with deliberate session settings and reset them afterwards.

Keep DDL transactions short. Retry lock-timeout failures from the migration runner in **new transactions**, with bounded attempts/backoff and inspection of the blocking cause. Do not put multi-step migrations in a long PL/pgSQL retry transaction; previous successful DDL may retain locks. Do not retry cast failures, integrity violations, partial multi-step state, or canceled concurrent builds as if nothing happened.

```sql
SELECT pid, state, query_start, pg_blocking_pids(pid) AS blockers
FROM pg_catalog.pg_stat_activity
WHERE datname = current_database() AND state <> 'idle';
```

Canceling someone else's backend requires authorization for that operational action. Existing authorization persists; ask only for a genuinely missing scope or decision. Source: [timeout settings](https://www.postgresql.org/docs/18/runtime-config-client.html).

## Validation and rollback

Use [validation queries](references/validation-queries.md) for pre/post conditions. Test on realistic data and inspect plans; do not equate a valid plan with correct business semantics. Follow the authorized data-access scope.

- A transaction rollback can undo transactional DDL before COMMIT. Concurrent index creation has separate committed phases and recovery checks.
- Reversing ADD COLUMN by dropping it is not data-preserving once the application has written values. A rollback plan must account for writes since deployment.
- DROP COLUMN/TABLE and lossy conversion require a tested recovery source; retain the old representation through the agreed rollback window. A backup does not itself prove recovery time or restore correctness.
- Dropping a column does not securely erase historical values from disk, WAL, replicas or backups. Ordinary VACUUM does not automatically rewrite every remaining live row to remove that column's storage.

For a complete example, see [adding a status column and index](references/complete-example.md). Forks/dump-restore test data-dependent behavior; separate concurrency tests cover writers, locks, retries and WAL pressure. Do not run an example against a live database merely because this skill is loaded.
