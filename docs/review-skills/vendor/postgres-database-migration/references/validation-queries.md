<!-- Locally adapted for Plainfin on 2026-11-04 from timescale/pg-aiguide commit 5d3b76b5c986693b30753828b404aa66ba34c31a. -->

# Migration Validation Queries

## Pre-Migration Validation

Run these checks **before** applying a migration. On a database fork, you can run them against real production data without any risk.

### Check for NULLs Before Adding NOT NULL

```sql
-- Will the NOT NULL constraint fail?
SELECT COUNT(*) AS null_count
FROM orders
WHERE order_status IS NULL;

-- Find sample rows to understand why they're NULL
SELECT id, created_at
FROM orders
WHERE order_status IS NULL
LIMIT 20;
```

### Check for Duplicates Before Adding UNIQUE

```sql
-- Will a UNIQUE constraint fail?
SELECT tracking_number, COUNT(*) AS occurrences
FROM orders
WHERE tracking_number IS NOT NULL
GROUP BY tracking_number
HAVING COUNT(*) > 1
ORDER BY occurrences DESC
LIMIT 20;
```

### Check for Orphans Before Adding a Foreign Key

```sql
-- Will a FK constraint fail?
SELECT o.id, o.user_id
FROM orders o
LEFT JOIN users u ON o.user_id = u.id
WHERE u.id IS NULL AND o.user_id IS NOT NULL
LIMIT 20;
```

### Check actual target conversion (PostgreSQL 18)

For types supporting soft input errors, check the complete target type and typmod. This is a value check, not `pg_typeof`, which only reports the expression's declared type. An arbitrary regex is not a substitute for the type's input function.

```sql
SELECT id, amount
FROM public.orders
WHERE amount IS NOT NULL
  AND NOT pg_input_is_valid(amount::text, 'numeric(12,2)')
LIMIT 20;
```

For NUMERIC, negative values are valid and scale conversion can round. After excluding invalid inputs in an isolated representative copy, check whether rounding is acceptable:

```sql
SELECT id, amount
FROM public.orders
WHERE amount IS NOT NULL
  AND amount::numeric IS DISTINCT FROM amount::numeric(12,2)
LIMIT 20;
```

Do not execute the second query while invalid values remain. For input types without soft-error support, `pg_input_is_valid` can itself raise an error; check support or exercise the actual conversion on an isolated copy. Source: [input validity functions](https://www.postgresql.org/docs/18/functions-info.html#FUNCTIONS-INFO-VALIDITY).

### Estimate Migration Duration

```sql
-- Table size and row count (estimate for planning)
SELECT
    pg_size_pretty(pg_total_relation_size('orders')) AS total_size,
    pg_size_pretty(pg_relation_size('orders')) AS data_size,
    reltuples::BIGINT AS estimated_rows
FROM pg_class
WHERE oid = 'public.orders'::regclass;

-- Estimate backfill time: run a small batch and extrapolate
-- WARNING: EXPLAIN ANALYZE actually executes the statement — this WILL update rows.
-- Run this on a fork, or wrap in a transaction and ROLLBACK after.
BEGIN;
EXPLAIN ANALYZE
UPDATE orders SET amount_new = amount::NUMERIC(12,2)
WHERE id BETWEEN 1 AND 1000 AND amount_new IS NULL;
-- If 1,000 rows takes 50ms and you have 10M rows → ~500s total
ROLLBACK;
```

## Post-Migration Validation

Run these **after** the migration to confirm it worked correctly.

### Schema Verification

```sql
-- Verify column was added/changed
SELECT column_name, data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_schema = 'public' AND table_name = 'orders' AND column_name = 'amount';

-- Verify constraint exists
SELECT conname, contype, pg_get_constraintdef(oid)
FROM pg_constraint
WHERE conrelid = 'orders'::regclass;

-- Verify index exists and is valid
SELECT indexrelid::regclass AS index_name,
       indisvalid AS is_valid,
       indisunique AS is_unique,
       pg_get_indexdef(indexrelid) AS definition
FROM pg_index
WHERE indrelid = 'orders'::regclass;
```

### Data Integrity

```sql
-- Verify backfill completed (no NULLs remaining)
SELECT COUNT(*) AS remaining_nulls
FROM orders
WHERE amount_new IS NULL AND amount IS NOT NULL;

-- Verify no data was lost
SELECT
    COUNT(*) AS total_rows,
    COUNT(amount) AS old_column_non_null,
    COUNT(amount_new) AS new_column_non_null
FROM orders;

-- Spot-check: old and new values match
SELECT id, amount AS old_value, amount_new AS new_value
FROM orders
WHERE amount::NUMERIC(12,2) IS DISTINCT FROM amount_new
LIMIT 10;
```

### Query Performance

```sql
-- Verify the new index is being used
EXPLAIN ANALYZE
SELECT * FROM orders WHERE user_id = 12345;

-- Check for sequential scans on the migrated table
SELECT relname, seq_scan, idx_scan
FROM pg_stat_user_tables
WHERE relname = 'orders';
```
