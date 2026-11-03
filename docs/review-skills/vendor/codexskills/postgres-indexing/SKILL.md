---
name: postgres-indexing
description: Designs and audits PostgreSQL indexes from real query plans - B-tree column order, partial, covering (INCLUDE), expression, GIN, GiST and BRIN indexes - builds them with CREATE INDEX CONCURRENTLY and drops unused or duplicate ones safely. Use when a query is slow, EXPLAIN shows a Seq Scan or a big Sort on a large table, a migration adds an index, a foreign key column lacks one, or someone asks which indexes are unused.
license: MIT
---

# Postgres indexing

An index is justified by a query plan, not by a column "looking important". Done well
means: every new index serves a named query and shows up in its plan, it was built
without blocking writes, and indexes nobody uses were removed after checking replicas.

## Before touching anything

1. Read `AGENTS.md` for how migrations are written (raw SQL, Rails, Django, Prisma,
   Alembic) and whether they run inside a transaction.
2. Get the exact query text with real parameter values, and the server version
   (`SELECT version();`). Index features differ: INCLUDE needs 11+, B-tree skip scan
   arrived in 18, `last_idx_scan` in 16.
3. Capture the current plan on production-like data. A plan from a 1,000-row dev table
   is worthless; the planner correctly prefers a Seq Scan there.

```sql
EXPLAIN (ANALYZE, BUFFERS, SETTINGS)
SELECT id, total FROM orders
WHERE customer_id = 4821 AND status = 'open'
ORDER BY created_at DESC LIMIT 20;
```

`ANALYZE` executes the statement. For `UPDATE`/`DELETE`, wrap it in `BEGIN; ... ROLLBACK;`
and still ask before running it against production.

4. List what already exists so you do not add a near-duplicate:

```sql
SELECT indexrelid::regclass AS index, pg_get_indexdef(indexrelid) AS def,
       pg_size_pretty(pg_relation_size(indexrelid)) AS size, idx_scan
FROM pg_stat_user_indexes JOIN pg_index USING (indexrelid)
WHERE relid = 'orders'::regclass ORDER BY idx_scan;
```

## Choosing the index type

| Query shape | Index | Notes |
|---|---|---|
| `=`, `<`, `>`, `BETWEEN`, `ORDER BY`, `IN` | B-tree (default) | Covers 90% of cases |
| Filter applies to a small, fixed subset (`WHERE deleted_at IS NULL`, `status = 'pending'`) | Partial B-tree | Query predicate must match the index predicate literally enough for the planner to prove it |
| Query only reads a few extra columns | B-tree with `INCLUDE (...)` | Enables Index Only Scan; needs a well-vacuumed table (visibility map) |
| `WHERE lower(email) = ...`, `date_trunc(...)` | Expression index | Query must use the identical expression |
| `jsonb @>`, array `@>`/`&&`, full-text `@@` | GIN | Slower writes; `fastupdate` pending list |
| Ranges, geometry, nearest neighbour, exclusion constraints | GiST (or SP-GiST) | `btree_gist` extension for mixed scalar + range |
| Huge append-only table filtered by time or a monotonically increasing id | BRIN | Tiny; only works when physical order tracks the column |
| `LIKE 'abc%'` under a non-C collation | B-tree with `text_pattern_ops` | Leading `%` needs `pg_trgm` GIN instead |

Hash indexes are rarely worth it over B-tree; skip unless measured.

## Multicolumn B-tree column order

Rule of thumb: equality columns first, then the range or sort column last.

```sql
-- WHERE customer_id = ? AND status = ? ORDER BY created_at DESC LIMIT 20
CREATE INDEX CONCURRENTLY orders_customer_status_created_idx
    ON orders (customer_id, status, created_at DESC);
```

- The index can satisfy `ORDER BY` only if the sort columns follow the equality columns
  in the same (or fully reversed) direction. Mixed directions need matching `DESC` in
  the definition.
- A trailing column used only by `IN (...)` or a range breaks ordering for columns after it.
- An index on `(a, b)` serves queries on `a` alone; a separate index on `(a)` is redundant.
- Before 18, a query on `b` alone cannot use `(a, b)` efficiently. 18's skip scan helps
  only when `a` has few distinct values; do not design around it.

## Partial and covering indexes

```sql
-- Only open orders are queried by the worker; they are 2% of the table.
CREATE INDEX CONCURRENTLY orders_open_created_idx
    ON orders (created_at) WHERE status = 'open';

-- Covering: the API reads id and total only.
CREATE INDEX CONCURRENTLY orders_customer_created_cov_idx
    ON orders (customer_id, created_at DESC) INCLUDE (total);
```

Partial index traps:
- `WHERE status = $1` with a generic prepared plan cannot use `WHERE status = 'open'`.
  The constant must appear in the query (ORMs that always parameterize can miss it).
- Rows that drift into the predicate later (status changes) are fine; the index is
  maintained on every update.

## Building indexes without an outage

Plain `CREATE INDEX` takes a SHARE lock: reads continue, every INSERT/UPDATE/DELETE waits
for the whole build. On any table that takes writes, always use:

```sql
SET lock_timeout = '5s';
CREATE INDEX CONCURRENTLY IF NOT EXISTS orders_customer_status_created_idx
    ON orders (customer_id, status, created_at DESC);
```

- `CONCURRENTLY` cannot run inside a transaction block. Rails: `disable_ddl_transaction!`
  plus `algorithm: :concurrently`. Django: `AddIndexConcurrently` in a migration with
  `atomic = False`. Alembic: `op.create_index(..., postgresql_concurrently=True)` inside
  `with op.get_context().autocommit_block():`.
- A failed concurrent build leaves an INVALID index that still slows writes. Find and
  drop it, then retry:

```sql
SELECT indexrelid::regclass FROM pg_index WHERE NOT indisvalid;
DROP INDEX CONCURRENTLY IF EXISTS orders_customer_status_created_idx;
```

- It waits for every transaction that could see the table to finish. A long-running
  transaction or idle-in-transaction session stalls the build; check `pg_stat_activity`.
- Unique constraints: build `CREATE UNIQUE INDEX CONCURRENTLY`, then
  `ALTER TABLE ... ADD CONSTRAINT ... UNIQUE USING INDEX ...`.
- Partitioned tables do not accept `CONCURRENTLY` on the parent: create the index
  `ON ONLY` the parent, build it concurrently on each partition, then
  `ALTER INDEX parent_idx ATTACH PARTITION child_idx`.
- Watch progress in `pg_stat_progress_create_index`.

## Foreign keys need their own index

Postgres indexes the referenced (parent) key, never the referencing column. Without an
index on `order_items.order_id`, every `DELETE FROM orders` scans `order_items`, and
joins from parent to child degrade. Find missing ones:

```sql
SELECT c.conrelid::regclass AS table, a.attname AS fk_column
FROM pg_constraint c
JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]
WHERE c.contype = 'f'
  AND NOT EXISTS (
    SELECT 1 FROM pg_index i
    WHERE i.indrelid = c.conrelid AND i.indkey[0] = c.conkey[1]);
```

(This checks single-column and leading-column coverage; review multi-column FKs by hand.)

## Finding unused and duplicate indexes

```sql
-- Candidates: never scanned since stats reset, not unique, not backing a constraint.
SELECT s.indexrelid::regclass AS index, pg_size_pretty(pg_relation_size(s.indexrelid)) AS size,
       s.idx_scan, s.last_idx_scan            -- last_idx_scan needs PG 16+
FROM pg_stat_user_indexes s JOIN pg_index i USING (indexrelid)
WHERE s.idx_scan = 0 AND NOT i.indisunique
  AND NOT EXISTS (SELECT 1 FROM pg_constraint c WHERE c.conindid = s.indexrelid)
ORDER BY pg_relation_size(s.indexrelid) DESC;

-- When were stats last reset? Zero scans over two days proves little.
SELECT stats_reset FROM pg_stat_database WHERE datname = current_database();
```

Before dropping:
- Statistics are per server. Check every read replica; an index unused on the primary
  may serve all reporting queries on a replica.
- Cover at least one full business cycle (month-end jobs, quarterly reports).
- Duplicates: same table, same leading columns, one a prefix of the other. Keep the
  wider one unless it is much larger and the narrow one is the hot path.
- Drop with `DROP INDEX CONCURRENTLY`, and keep the `CREATE` statement in the PR so it
  can be restored. Dropping is destructive on shared environments: confirm with the
  user before running it outside a migration review.

## Gotchas

- Functions or casts on the column (`WHERE created_at::date = ...`,
  `WHERE date_trunc('day', created_at) = ...`, `WHERE col::text = ...`) prevent use of a
  plain index. Rewrite the predicate as a range on the raw column, or add an expression
  index if the expression is genuinely the access path.
- `OR` across different columns often falls back to a Seq Scan or BitmapOr; a
  `UNION ALL` of two indexed queries can be faster.
- Low-selectivity columns (boolean, a status with 3 values evenly spread) rarely benefit
  from a standalone index; use them as partial predicates instead.
- Every index slows writes and prevents HOT updates on the columns it covers. A table
  with 15 indexes and a write-heavy workload needs pruning more than another index.
- Bloated indexes after heavy churn: `REINDEX INDEX CONCURRENTLY` (12+), not plain REINDEX.
- After bulk loads, run `ANALYZE table` so the planner sees the new distribution.

## Verification

- [ ] Plan before and after captured with `EXPLAIN (ANALYZE, BUFFERS)` on realistic data
- [ ] Target node switched to Index Scan / Index Only Scan / Bitmap Index Scan on the new index
- [ ] `shared read` buffers and execution time dropped; heap fetches near zero for Index Only Scans
- [ ] `SELECT indisvalid FROM pg_index WHERE indexrelid = 'new_idx'::regclass` returns true
- [ ] Write-path cost considered (table write rate, total index count)
- [ ] For drops: zero scans confirmed on primary and all replicas over a full cycle

If the planner still ignores the index, check in this order: stale statistics
(`ANALYZE`), predicate mismatch (cast, function, parameterized partial predicate),
estimate far from actual rows (see `$postgres-query-tuning`), and `random_page_cost`
still at the spinning-disk default of 4 on SSD storage.

## Output template

```markdown
## Index change: <table>
**Query served:** <normalized query or endpoint>
**Before:** <node type>, <rows>, <time> ms, <buffers>
**Index:** `CREATE INDEX CONCURRENTLY ... ;`
**After:** <node type>, <time> ms, <buffers>
**Write cost:** <table writes/s, index count before -> after>
**Dropped:** <index, size, evidence of non-use incl. replicas> or none
**Rollback:** `DROP INDEX CONCURRENTLY ...;`
```
