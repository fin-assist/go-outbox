---
name: postgres-query-tuning
description: Diagnoses slow PostgreSQL queries by reading EXPLAIN (ANALYZE, BUFFERS) plans node by node - misestimated row counts, nested loops over large inputs, sorts and hashes spilling to disk, stale or missing statistics - then fixes them with ANALYZE, extended statistics, query rewrites, keyset pagination or targeted settings. Uses pg_stat_statements to find what to tune first. Use when a Postgres query or endpoint is slow, a plan changed after a deploy, or someone pastes an EXPLAIN output and asks why.
license: MIT
---

# Postgres query tuning

Tune the query that costs the most total time, prove the cause in the plan, change one
thing, and measure again on production-like data. A fix is done when the plan explains
the speed-up (fewer rows, fewer buffers, a better join), not when one run got lucky
with a warm cache.

## Step 1: pick the right query

Do not start from a hunch. With `pg_stat_statements` enabled (it needs
`shared_preload_libraries` and `CREATE EXTENSION pg_stat_statements`):

```sql
SELECT queryid, calls, round(total_exec_time) AS total_ms,
       round(mean_exec_time, 1) AS mean_ms, rows,
       shared_blks_read, temp_blks_written, left(query, 120) AS query
FROM pg_stat_statements
ORDER BY total_exec_time DESC LIMIT 15;
```

- High `total_ms`, low `mean_ms`, huge `calls`: an N+1 or chatty loop. The fix is in
  application code (`$n-plus-one-queries`), not the planner.
- High `mean_ms`: a genuinely slow plan. Continue below.
- `temp_blks_written` > 0: sorts or hashes spill to disk.

Read `AGENTS.md` for how to reach a production-like database. Never run `EXPLAIN ANALYZE`
on a data-modifying statement in production without `BEGIN ... ROLLBACK` and the user's
approval; ANALYZE really executes it.

## Step 2: capture a real plan

```sql
EXPLAIN (ANALYZE, BUFFERS, SETTINGS, VERBOSE)  -- PG 18 adds BUFFERS by default
SELECT ...;
```

- Use the real parameter values from the slow case, not convenient ones. Skewed values
  (a customer with 2M orders) produce different plans.
- Run twice. The first run may read from disk; compare `shared hit` vs `shared read`.
- For huge plans use `FORMAT JSON` and paste into a visualizer offline, or read it
  bottom-up yourself.
- Prepared statements can use a generic plan after five executions. To reproduce,
  `PREPARE` the query and `EXPLAIN ANALYZE EXECUTE` it six or more times, or compare
  with `SET plan_cache_mode = force_custom_plan`.

## Step 3: read the plan

Read from the innermost (most indented) nodes outward. For each node compare:

| Signal | What it means | Typical fix |
|---|---|---|
| `rows=` estimate vs `actual rows` off by 10x or more | Planner is guessing; everything above it is built on the bad guess | Statistics (step 4) |
| `Nested Loop` with large `loops=` on the inner side | Bad estimate made the planner expect few outer rows | Fix estimate; index the inner join key |
| `Seq Scan` with `Rows Removed by Filter` huge | No usable index, or predicate not sargable | Index or rewrite (`$postgres-indexing`) |
| `Sort Method: external merge Disk: ...` | `work_mem` too small for this sort | Index providing the order, or raise `work_mem` for this query |
| `Hash ... Batches: 8` (more than 1) | Hash spilled | Same as above |
| `Heap Fetches: N` large on Index Only Scan | Visibility map stale | `VACUUM` the table; tune autovacuum |
| `Buffers: shared read=` huge | Data not cached, query touches too many pages | Narrow the scan; covering index |
| `Bitmap Heap Scan ... Recheck` with `lossy` | Bitmap exceeded `work_mem` | More selective index or more `work_mem` |
| `InitPlan` / `SubPlan` executed per row | Correlated subquery | Rewrite as join or `LATERAL` |

Remember that `actual time` and `rows` are per loop; multiply by `loops` for totals.

## Step 4: fix estimates before rewriting

```sql
ANALYZE orders;                                   -- stale stats after bulk load
SELECT last_analyze, last_autoanalyze, n_mod_since_analyze
FROM pg_stat_user_tables WHERE relname = 'orders';

ALTER TABLE orders ALTER COLUMN customer_id SET STATISTICS 1000;  -- skewed column
ANALYZE orders;

-- Correlated columns (city and zip, tenant and status): the planner multiplies
-- selectivities as if independent and underestimates.
CREATE STATISTICS orders_tenant_status (dependencies, ndistinct, mcv)
    ON tenant_id, status FROM orders;
ANALYZE orders;
```

- Expressions in WHERE (`lower(email)`, `(data->>'kind')`) have no statistics unless an
  expression index or expression statistics (`CREATE STATISTICS ... ON (lower(email))`,
  PG 14+) exists.
- Large tables with fast churn: autoanalyze triggers at 10% change by default. Lower
  `autovacuum_analyze_scale_factor` per table (e.g. 0.01) via `ALTER TABLE ... SET (...)`.

## Step 5: rewrite when the query shape is the problem

| Pattern | Rewrite |
|---|---|
| `OFFSET 100000 LIMIT 50` | Keyset: `WHERE (created_at, id) < ($1, $2) ORDER BY created_at DESC, id DESC LIMIT 50` |
| `WHERE a = 1 OR b = 2` on different columns | `UNION ALL` of two indexed queries; make the second branch `WHERE b = 2 AND a IS DISTINCT FROM 1` so rows are not returned twice |
| `NOT IN (SELECT ...)` | `NOT EXISTS (...)`; NOT IN also returns no rows if the subquery yields a NULL |
| `WHERE created_at::date = '2026-03-01'` | `WHERE created_at >= '2026-03-01' AND created_at < '2026-03-02'` |
| `SELECT ... WHERE id IN (<10,000 literals>)` | `= ANY($1::bigint[])` or join a `VALUES`/temp table |
| `COUNT(*)` for "is there any" | `EXISTS (SELECT 1 ...)` |
| Top-N per group via correlated subquery | `LATERAL (... ORDER BY ... LIMIT n)` with a supporting index |
| CTE referenced twice that should be inlined | Since PG 12 CTEs are inlined only when referenced once; add `NOT MATERIALIZED` if inlining helps |
| Big aggregate recomputed per request | Materialized view refreshed `CONCURRENTLY`, or a summary table |

Keep the rewrite semantically identical: check NULL handling, duplicates (`UNION` vs
`UNION ALL`) and ordering. Diff the result sets of old and new queries on real data:

```sql
(SELECT * FROM old_q EXCEPT ALL SELECT * FROM new_q)
UNION ALL
(SELECT * FROM new_q EXCEPT ALL SELECT * FROM old_q);   -- must return zero rows
```

## Step 6: settings, last and narrowly

Change settings only when the plan shows the reason, and prefer the narrowest scope:
`SET LOCAL` inside a transaction, then per role (`ALTER ROLE reporting SET work_mem = '256MB'`),
and only then the server.

| Setting | When | Note |
|---|---|---|
| `work_mem` | Sorts/hashes spill | Allocated per sort/hash node per backend; 100 connections x 4 nodes x 256MB can exhaust RAM |
| `random_page_cost` | SSD/NVMe still at default 4 | 1.1 is common on SSD; affects index vs seq scan choice everywhere |
| `effective_cache_size` | Planner undervalues index scans | Roughly 50-75% of RAM; it allocates nothing |
| `jit` | Short OLTP queries showing large `JIT:` timing | `SET jit = off` for the role or app |
| `enable_nestloop = off` etc. | Diagnosis only | Never ship; it proves a hypothesis, then fix the estimate |

Managed services (RDS, Cloud SQL, Neon, Supabase) change server settings through
parameter groups or dashboards; tell the user instead of attempting `ALTER SYSTEM`.

## Gotchas

- `LIMIT` with `ORDER BY` on an indexed column can make the planner walk an index hoping
  to find matches early; when matches are rare it reads millions of rows. Fix with a
  composite index matching filter plus order, or stats on the filter column.
- Timing overhead: `EXPLAIN ANALYZE` on very fast nodes inflates time. Use
  `TIMING OFF` to check whether instrumentation itself dominates.
- A plan that is fast with a warm cache may still be slow in production under memory
  pressure. Compare buffers read, not only milliseconds.
- Parameter type mismatches from drivers (numeric parameter against an integer column,
  text against varchar with a different collation) can disable index use. Check the
  `Filter:` line for unexpected casts.
- Lock waits look like slow queries. If `EXPLAIN ANALYZE` is fast but production is
  slow, check `pg_stat_activity.wait_event_type` = `Lock` (`$database-transactions`).

## Verification

- [ ] Query chosen from `pg_stat_statements` or a reproducible slow request
- [ ] Before plan saved with ANALYZE and BUFFERS, using real parameters
- [ ] Root cause named against a specific plan node
- [ ] One change applied at a time; after plan captured the same way
- [ ] Rewritten queries return identical results (EXCEPT ALL diff empty)
- [ ] `pg_stat_statements` mean time confirmed lower after deploy (`pg_stat_statements_reset()` only with user approval, it clears shared history)

## Output template

```markdown
## Query tuning: <query id or endpoint>
**Cost before:** <calls/day>, mean <ms>, total share <%>
**Root cause:** <node> estimated <n> rows, actual <m>; <consequence>
**Change:** <ANALYZE / statistics / index / rewrite / setting>, with SQL
**Plan after:** <node change>, <ms>, <buffers>
**Result check:** <how equivalence was verified>
**Risks:** <write cost, setting scope, plan stability>
```
