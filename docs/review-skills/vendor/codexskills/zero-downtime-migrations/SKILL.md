---
name: zero-downtime-migrations
description: Plans and writes database schema migrations that run while the app serves traffic - expand/contract sequencing across deploys, lock_timeout guards, NOT VALID constraints, concurrent index builds, safe column renames and type changes, batched backfills - mainly for PostgreSQL, with notes for MySQL. Reviews Rails, Django, Prisma, Alembic and raw SQL migrations for statements that take long ACCESS EXCLUSIVE locks or rewrite tables. Use when writing or reviewing a migration against a large or busy table, renaming or retyping a column, adding NOT NULL or a foreign key, or planning a multi-deploy schema change.
license: MIT
---

# Zero-downtime migrations

A migration is safe when every statement either finishes in milliseconds or holds only
locks that let reads and writes continue, when old and new application versions both
work against every intermediate schema, and when each step can be stopped or reverted.
"It ran fine locally" proves nothing: the dev table has 50 rows and no traffic.

## The lock queue problem

Most outages come from short locks, not long ones. `ALTER TABLE` needs an ACCESS
EXCLUSIVE lock. If a 10-minute report query holds a lock on the table, the ALTER waits,
and every later query on that table queues behind the ALTER. The site is down for 10
minutes because of a statement that would take 5 ms. Always guard DDL:

```sql
SET lock_timeout = '3s';         -- give up instead of queueing everyone behind us
SET statement_timeout = '15min'; -- for the statement itself (raise for index builds)
```

Retry a timed-out step a few times with backoff; if it keeps failing, find the blocker
(`SELECT pid, now() - xact_start, state, query FROM pg_stat_activity ORDER BY 2 DESC`).

## Workflow

1. Read `AGENTS.md` and the migration tool docs used in the repo. Note whether each
   migration runs in a transaction (Rails, Django, Alembic, Flyway do by default) and
   how deploys order "migrate" vs "release new code".
2. Get table facts: `SELECT pg_size_pretty(pg_total_relation_size('t')), n_live_tup FROM pg_stat_user_tables WHERE relname = 't';`
   and its write rate. Under ~10k rows most things are safe; above 1M, treat every
   statement as dangerous until checked against the lock table below.
3. Classify each statement with [references/lock-matrix.md](references/lock-matrix.md)
   (read it for any statement not covered in the quick table).
4. Split into expand / migrate / contract phases, each its own deploy.
5. Add guards (`lock_timeout`), concurrency flags and batching.
6. Run the migration against a production-sized copy or at least a table with millions
   of seeded rows, and time each statement.
7. Present the deploy plan to the user. Running migrations against production, and any
   contract step that drops columns or tables, requires the user's explicit go-ahead.

## Quick safety table (PostgreSQL 12+)

| Change | Safe? | Safe recipe |
|---|---|---|
| Add nullable column, or with a constant default | Yes (11+, metadata only) | Plain `ADD COLUMN` with lock_timeout |
| Add column with volatile default (`clock_timestamp()`, `gen_random_uuid()`) | No, rewrites table | Add nullable, backfill in batches, then set default |
| Add NOT NULL to existing column | Scans table under ACCESS EXCLUSIVE | CHECK NOT VALID, VALIDATE, SET NOT NULL, drop CHECK (below) |
| Add foreign key | Locks both tables while scanning | `ADD CONSTRAINT ... NOT VALID`, then `VALIDATE CONSTRAINT` |
| Add CHECK | Scans under lock | `NOT VALID`, then `VALIDATE` |
| Add index | Blocks writes | `CREATE INDEX CONCURRENTLY` outside a transaction |
| Add unique constraint | Blocks writes | Unique index concurrently, then `ADD CONSTRAINT ... UNIQUE USING INDEX` |
| Change column type | Usually rewrites table | New column + dual write + backfill + swap (expand/contract) |
| Rename column or table | Instant, but breaks running code | Expand/contract, or a view/compat layer |
| Drop column | Instant, but breaks code still selecting it | Remove from code first (and ORM ignored-columns), then drop |
| Drop table | Instant, irreversible | Rename to `_deprecated`, wait a release, then drop with approval |
| Set or drop default | Instant | Fine |

## Recipes

### NOT NULL on a big table

```sql
SET lock_timeout = '3s';
ALTER TABLE orders ADD CONSTRAINT orders_customer_id_not_null
    CHECK (customer_id IS NOT NULL) NOT VALID;            -- instant
-- separate transaction / migration:
ALTER TABLE orders VALIDATE CONSTRAINT orders_customer_id_not_null;  -- scans, allows writes
ALTER TABLE orders ALTER COLUMN customer_id SET NOT NULL; -- PG 12+ uses the valid CHECK, no scan
ALTER TABLE orders DROP CONSTRAINT orders_customer_id_not_null;
```

On PG 18 you can instead add `NOT NULL` itself as `NOT VALID` and validate it later;
use the CHECK route when the fleet has older versions.

### Foreign key

```sql
ALTER TABLE order_items ADD CONSTRAINT order_items_order_id_fkey
    FOREIGN KEY (order_id) REFERENCES orders (id) NOT VALID;
ALTER TABLE order_items VALIDATE CONSTRAINT order_items_order_id_fkey;
```

Create the index on `order_items (order_id)` concurrently first, or deletes on `orders`
will scan `order_items`.

### Rename or retype a column (expand/contract)

| Deploy | Schema | Application |
|---|---|---|
| 1 Expand | Add `email_normalized` (nullable) | Write both columns; read old |
| 2 Migrate | Batched backfill; add constraints NOT VALID then validate | Write both; read old |
| 3 Switch | (none) | Read new; still write both |
| 4 Contract | Drop old column after one release with no reads | Write new only |

A trigger can do the dual write in deploy 1 if several services write the table; it
must be removed in the contract phase.

### Backfills

Never `UPDATE big_table SET x = ...` in one statement: it holds row locks for the whole
run, bloats the table and can build replication lag. Batch by primary key, commit per
batch, and throttle:

```sql
UPDATE orders SET email_normalized = lower(email)
WHERE id IN (
  SELECT id FROM orders
  WHERE id > :last_id AND email_normalized IS NULL
  ORDER BY id LIMIT 5000)
RETURNING id;   -- carry max(id) forward as :last_id; sleep between batches
```

Monitor replica lag and stop if it climbs. Large or long-running backfills belong in a
job, not a migration: see `$data-backfills`.

## Framework notes

- **Rails**: `disable_ddl_transaction!` for concurrent indexes; the `strong_migrations`
  gem flags most unsafe operations. Add removed columns to `self.ignored_columns` before
  dropping them.
- **Django**: `AddIndexConcurrently` / `RemoveIndexConcurrently` from
  `django.contrib.postgres.operations` with `atomic = False`. Use `SeparateDatabaseAndState`
  to deploy code and schema changes in different releases. Review with `sqlmigrate`.
- **Alembic**: `with op.get_context().autocommit_block():` for concurrent statements;
  inspect SQL with `alembic upgrade head --sql`.
- **Prisma / Drizzle**: generated SQL is plain DDL with no lock guards. Edit the
  generated migration file (Prisma: `prisma migrate dev --create-only`) to add
  `CONCURRENTLY`, `NOT VALID` and `lock_timeout`; concurrent index builds must not sit
  in a transaction.
- **Linting**: `squawk` lints raw Postgres migration SQL for these patterns; run it in CI
  if the repo uses SQL files. For expand/contract with automatic views, `pgroll` is an option.

## MySQL differences

- Prefer `ALGORITHM=INSTANT` (8.0.29+ supports add/drop column at any position) or
  `ALGORITHM=INPLACE, LOCK=NONE`, and state them explicitly so MySQL errors instead of
  silently falling back to a table copy.
- Metadata locks behave like the Postgres lock queue: set `lock_wait_timeout` low for
  the DDL session.
- For large tables that need a copy, use gh-ost or pt-online-schema-change (`$mysql-online-schema-change`).

## Gotchas

- Mixing a concurrent index build with other statements in one migration file fails or
  silently runs non-concurrently, depending on the tool. One concurrent statement per migration.
- Enum value additions (`ALTER TYPE ... ADD VALUE`) cannot be used in the same transaction.
- `DEFAULT` changes apply only to new rows; existing NULLs still need a backfill.
- Deploying code that reads a new column before the migration ran causes errors on the
  first instances to boot. Order: migrate (expand) before deploy; contract after.
- Long-running transactions block concurrent index builds and VALIDATE from finishing.
- Rollbacks: a `down` that recreates a dropped column does not restore its data.
  Treat contract steps as irreversible and take a backup or snapshot first.

## Verification

- [ ] Every statement classified by lock level and rewrite behaviour
- [ ] `lock_timeout` set for every DDL; concurrent operations outside transactions
- [ ] Old app version works against the post-migration schema (run its test suite or smoke test against it)
- [ ] Timed on production-sized data; longest lock-holding statement under ~1 s
- [ ] Invalid indexes checked after concurrent builds (`SELECT * FROM pg_index WHERE NOT indisvalid`)
- [ ] Destructive contract steps listed separately and approved by the user

## Output template

```markdown
## Migration plan: <change>
**Table:** <name>, <rows>, <size>, <writes/s>
| Step | Deploy | SQL | Lock | Duration (measured) | Revert |
|---|---|---|---|---|---|
**App compatibility:** <which app versions work with which step>
**Backfill:** <batch size, throttle, est. time, monitoring>
**Needs approval:** <drops, production run>
```
