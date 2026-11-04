<!-- Locally adapted for Plainfin on 2026-11-04 from timescale/pg-aiguide commit 5d3b76b5c986693b30753828b404aa66ba34c31a. -->

# Bounded backfills with durable checkpoints

These examples assume `public.orders(id bigint primary key, amount numeric, amount_new numeric(12,2))`. Select names and conversion semantics for the real schema. Source NULL maps to target NULL; if the target must be NOT NULL, resolve source NULLs as a domain decision before the migration.

## Writer contract first

Before a live backfill, either pause all writers or deploy atomic dual-writing in the application and drain old writer versions. New/updated rows must keep both representations consistent. A checkpoint alone cannot protect against an old writer changing an already-copied row. Check for conversion errors and rounding before writing.

## One batch per transaction

Use keyset selection of existing IDs, not arithmetic windows. `$1` is the last processed ID (NULL for the first batch, including negative IDs), `$2` the captured upper ID, `$3` the positive batch limit. Capture the upper bound only after the writer contract is active; it bounds this pass, not a lifetime guarantee that later writes need no validation.

```sql
WITH batch AS MATERIALIZED (
  SELECT id
  FROM public.orders
  WHERE ($1::bigint IS NULL OR id > $1)
    AND id <= $2::bigint
    AND amount IS NOT NULL
    AND amount_new IS NULL
  ORDER BY id
  LIMIT $3::integer
  FOR UPDATE
), updated AS (
  UPDATE public.orders AS o
  SET amount_new = o.amount::numeric(12,2)
  FROM batch AS b
  WHERE o.id = b.id
  RETURNING o.id
)
SELECT count(*) AS rows_updated, max(id) AS next_last_id
FROM updated;
```

Choose a modest initial batch size, use transaction-local lock/statement timeouts, and measure elapsed time/WAL/lag. Commit between batches; a sequence of small UPDATE statements inside one outer transaction is not a bounded-transaction backfill. Do not use SKIP LOCKED with a high-water checkpoint unless a separate reconciliation mechanism tracks skipped IDs: advancing beyond locked rows would omit them.

## Resuming

Use the existing migration runner's durable progress mechanism where available. If a progress table is needed, the runner must store `last_id` (nullable initially), `upper_id`, a completed flag and migration identity. Do not initialize last_id to zero unless IDs are known nonnegative. A transaction does the following:

1. Lock this migration's progress record with `SELECT ... FOR UPDATE` so two runners cannot advance it independently.
2. Run the batch above using that record's last_id and upper_id.
3. If rows were updated, store the returned actual `next_last_id` and increment the counter **in the same transaction**. Then COMMIT.
4. If the next selection is empty, the current pass is complete. Reconcile the table under the writer contract before marking the migration accepted. A future row may still appear if writers were not constrained as required.

A failed transaction rolls back both the batch and its checkpoint; resume from the last committed record. Sleep/backoff happens after COMMIT in application code. Avoid a `DO` block with internal COMMIT: it depends on the runner's transaction context and does not solve synchronization.

## Final reconciliation

After all planned batches, and before switching readers, check both missing and incorrect values, not only NULL counts:

```sql
SELECT id
FROM public.orders
WHERE amount_new IS DISTINCT FROM amount::numeric(12,2)
ORDER BY id
LIMIT 20;
```

A nonempty result is not acceptance. Diagnose/reconcile under the same writer strategy, then repeat bounded validation. If converting to exact financial values, validate permitted rounding separately; a successful cast does not imply lossless conversion.

Sources: [SELECT / row locks](https://www.postgresql.org/docs/18/sql-select.html), [transaction isolation](https://www.postgresql.org/docs/18/transaction-iso.html).
