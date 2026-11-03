---
name: data-backfills
description: Plans and runs large data backfills on live databases - keyset-batched UPDATE and INSERT ... SELECT jobs with per-batch transactions, checkpoints for resume, throttling on replica lag and load, kill switches, and before/after verification queries - for PostgreSQL and MySQL, via raw SQL, Python, Rails or Django jobs. Use when populating a new column across millions of rows, copying data into a new table, recomputing a derived field, fixing corrupted rows in bulk, or when a migration backfill is too slow or locks the table.
license: MIT
---

# Data backfills

Done well means: the backfill can be stopped and resumed at any point without harm,
every batch is short enough that nobody notices it (no lock queues, no replica lag, no
latency bump), new writes are already handled before it starts, and a query proves zero
rows were missed when it ends.

## Before writing the job

1. Read `AGENTS.md` and existing jobs; find how the repo runs one-off tasks (rake task,
   management command, job queue, maintenance-task framework).
2. Measure the target: total rows, rows needing change, table and index size, write rate.

```sql
SELECT reltuples::bigint AS approx_rows,
       pg_size_pretty(pg_total_relation_size('orders')) AS total_size
FROM pg_class WHERE relname = 'orders';
SELECT count(*) FROM orders WHERE region_id IS NULL;   -- on a replica if huge
```

3. Make new writes correct first. Deploy the code that writes the new value (dual write,
   trigger, or default) before backfilling, or rows created during the run are missed.
4. List side effects of touching each row: triggers (audit, `updated_at`), CDC or outbox
   consumers, search indexing, cache invalidation, ORM callbacks. A million updates can
   mean a million Kafka events.
5. Agree on a window, a throttle and an abort condition with the user. Running against
   production needs explicit confirmation of the exact command and target.

## Choose the shape

| Situation | Approach |
|---|---|
| Set a column on existing rows | Batched `UPDATE ... WHERE id range AND <needs change>` |
| Copy into a new table (re-key, split, merge) | Batched `INSERT ... SELECT ... ON CONFLICT DO NOTHING` |
| Rewrite most of a very large table | Build a new table, backfill, swap in a short lock (see `$zero-downtime-migrations`) |
| Needs app logic (API calls, parsing) | Job per batch of ids through the job queue |
| Fewer than about 50k rows, quiet table | Single statement with `lock_timeout` is fine |

## Batching rules

- **Keyset, never OFFSET.** Walk the primary key (or another indexed, unique, monotonic
  key): `WHERE id > :last_id ORDER BY id LIMIT :n`. OFFSET rescans skipped rows and gets
  slower each batch.
- **Idempotent predicate.** Include the "still needs work" condition
  (`AND region_id IS NULL`, `AND version < 2`) so reruns skip finished rows.
- **One transaction per batch.** Never wrap the whole backfill in one transaction: it holds
  locks, blocks vacuum and makes the whole thing all-or-nothing.
- **Target batch duration, not batch size.** Aim for 100-500 ms per batch. Start at 1,000
  rows, measure, adjust. Sparse matches mean ranges by id beat `LIMIT` on the predicate.
- **Guard every batch** with `SET LOCAL lock_timeout = '2s'` and `statement_timeout`.
- **Checkpoint** the last processed key to a table or durable store after each commit.

## Reference implementation (Postgres, psycopg 3)

```python
import os, sys, time
import psycopg

BATCH, PAUSE, MAX_LAG_S = 2000, 0.05, 5.0
JOB = "orders_region_backfill"

def replica_lag(conn) -> float:
    row = conn.execute("""SELECT coalesce(max(extract(epoch FROM replay_lag)), 0)
                          FROM pg_stat_replication""").fetchone()
    return float(row[0])

def stop_requested(conn) -> bool:
    row = conn.execute("SELECT stop FROM backfill_control WHERE job = %s", (JOB,)).fetchone()
    return bool(row and row[0])

with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
    last = conn.execute("SELECT last_id FROM backfill_progress WHERE job = %s",
                        (JOB,)).fetchone()
    last_id = last[0] if last else 0
    max_id = conn.execute("SELECT max(id) FROM orders").fetchone()[0] or 0
    while last_id < max_id:
        if stop_requested(conn):
            sys.exit(f"stopped by kill switch at id {last_id}")
        while replica_lag(conn) > MAX_LAG_S:
            time.sleep(5)
        upper = last_id + BATCH
        started = time.monotonic()
        with conn.transaction():
            conn.execute("SET LOCAL lock_timeout = '2s'")
            conn.execute("SET LOCAL statement_timeout = '30s'")
            cur = conn.execute("""
                UPDATE orders o SET region_id = c.region_id
                FROM customers c
                WHERE o.customer_id = c.id
                  AND o.id > %s AND o.id <= %s
                  AND o.region_id IS NULL""", (last_id, upper))
            conn.execute("""
                INSERT INTO backfill_progress (job, last_id, updated_at)
                VALUES (%s, %s, now())
                ON CONFLICT (job) DO UPDATE SET last_id = EXCLUDED.last_id,
                                                updated_at = now()""", (JOB, upper))
        last_id = upper
        took = time.monotonic() - started
        print(f"id<={upper} updated={cur.rowcount} {took*1000:.0f}ms", flush=True)
        time.sleep(PAUSE + took)    # yield at least as long as we worked
```

`pg_stat_replication` is only visible on the primary and needs `pg_monitor` (or
superuser) to show lag columns; on managed services read lag from the provider's metric
instead. `max_id` is captured once; rows inserted later are covered by step 3.

### Pure SQL alternative

Postgres 11+ procedures can commit between batches; run with `CALL` outside a transaction:

```sql
CREATE PROCEDURE backfill_region(batch int DEFAULT 2000) LANGUAGE plpgsql AS $$
DECLARE lo bigint := 0; hi bigint;
BEGIN
  SELECT max(id) INTO hi FROM orders;
  WHILE lo < hi LOOP
    UPDATE orders o SET region_id = c.region_id FROM customers c
     WHERE o.customer_id = c.id AND o.id > lo AND o.id <= lo + batch
       AND o.region_id IS NULL;
    lo := lo + batch;
    COMMIT;
    PERFORM pg_sleep(0.05);
  END LOOP;
END $$;
```

It cannot watch replica lag or resume from a checkpoint; prefer the script for anything
that runs longer than a few minutes.

### MySQL notes

- Same keyset loop; check lag with `SHOW REPLICA STATUS` (`Seconds_Behind_Source`, 8.0.22+)
  or a heartbeat table (`pt-heartbeat`) for accuracy.
- Large `UPDATE` with a range on InnoDB takes next-key locks on the range; keep batches
  small and the predicate on the primary key.
- `innodb_lock_wait_timeout` per session (`SET SESSION innodb_lock_wait_timeout = 5`).

### Framework jobs

```ruby
# Rails: each batch is one UPDATE, no callbacks or validations
Order.where(region_id: nil).in_batches(of: 2000) do |batch|
  batch.update_all("region_id = (SELECT region_id FROM customers WHERE customers.id = orders.customer_id)")
  sleep 0.05
end
```

For long Rails backfills prefer a resumable framework (the `maintenance_tasks` gem) over a
rake task that dies with the SSH session. In Django, iterate primary-key ranges and call
`QuerySet.filter(pk__gt=lo, pk__lte=hi, region_id__isnull=True).update(...)`; never
`for obj in Model.objects.all(): obj.save()`.

## Run it

```
- [ ] Dry run on a production-sized copy; record rows/s and per-batch latency
- [ ] Estimate duration: remaining rows / measured rate; agree on a window
- [ ] Create backfill_progress and backfill_control tables (or equivalent)
- [ ] Get explicit go-ahead for production; start in tmux or as a tracked job
- [ ] Watch: replica lag, p95 API latency, lock waits, dead tuples, CDC consumer lag
- [ ] On anomaly: flip the kill switch, investigate, resume from checkpoint
```

Dead tuples accumulate with every update. Watch `n_dead_tup` in `pg_stat_user_tables`; if
autovacuum falls behind, slow the job or run `VACUUM (ANALYZE) orders` between phases.

## Verification

```sql
-- nothing left behind (rows that should have changed)
SELECT count(*) FROM orders o JOIN customers c ON c.id = o.customer_id
WHERE o.region_id IS DISTINCT FROM c.region_id;

-- spot check against the source of truth
SELECT o.id, o.region_id, c.region_id FROM orders o
JOIN customers c ON c.id = o.customer_id
ORDER BY random() LIMIT 20;
```

- Remaining count is 0, including rows inserted during the run.
- Totals or checksums match where data was copied:
  `SELECT count(*), sum(amount_cents) FROM old` vs `new`.
- Only after verification: add the `NOT NULL` constraint or switch reads (`$zero-downtime-migrations`).

## Gotchas

- Updating an indexed column, or any column on a table without HOT room, writes index
  entries for every row: expect WAL volume roughly the size of the table plus indexes.
- Triggers that bump `updated_at` make every row look recently changed to sync jobs.
- `ORDER BY random()` or `LIMIT` without `ORDER BY` in batch selection skips or repeats rows.
- Non-monotonic UUIDv4 keys still work for keyset walking (ordering is stable), but ranges
  are uneven; batch by `LIMIT` on `id > last` instead of fixed numeric ranges.
- A backfill run from a laptop dies with Wi-Fi. Run it where it can survive (a job, a
  pod, tmux on a bastion).

## Report template

```markdown
**Target:** orders.region_id, 84.2M rows, 61.0M needing update
**Method:** keyset batches of 2,000 by id, 50 ms pause plus adaptive sleep, lag cap 5 s
**Pre-reqs done:** dual write deployed in v412; audit trigger kept (about +12% WAL)
**Run:** 6 h 40 min, 2,540 rows/s avg, paused twice for lag (max 4.1 s)
**Verified:** mismatch count 0; 20-row spot check OK; NOT NULL validated after
**Leftovers:** drop backfill_progress after one week
```
