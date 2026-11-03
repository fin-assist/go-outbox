---
name: postgres-partitioning
description: Designs and operates PostgreSQL declarative partitioning - choosing RANGE, LIST or HASH keys, partition size and count, primary and unique keys that include the partition key, partition pruning checks with EXPLAIN, pg_partman or scripted creation of future partitions, retention by DETACH PARTITION CONCURRENTLY, and converting an existing large table online. Use when a Postgres table grows into hundreds of millions of rows, old data must be dropped on a schedule, VACUUM or index maintenance on one huge table hurts, or someone asks whether to partition.
license: MIT
---

# Postgres partitioning

Partition for a reason you can name: dropping old data instantly, keeping vacuum and
index maintenance per-chunk, or pruning scans to a small slice. Done well means the
hot queries prune to one or a few partitions, future partitions always exist before
data arrives, and retention is a metadata operation instead of a giant DELETE.

## Should this table be partitioned?

| Situation | Partition? |
|---|---|
| Time-series or event data with retention ("keep 13 months") | Yes, RANGE by time. The main win |
| Table > ~100 GB where most queries hit recent data | Usually yes |
| Multi-tenant table where a few huge tenants dominate, and tenants get offboarded | LIST by tenant for the big ones, or HASH; weigh carefully |
| Table < ~50 GB, queries already use good indexes | No. Fix indexes first (`$postgres-indexing`) |
| Queries rarely filter on the would-be partition key | No. Every query would scan every partition |
| You want faster point lookups by primary key | No. Partitioning does not speed those up |

Partitioning adds planning overhead, constraints on unique keys and operational work.
If the reason is "it is big", say so and push back.

## Choosing the key and granularity

- The key must appear in the WHERE clause of the hot queries, as a plain comparison
  on the column (`created_at >= $1`), not wrapped in a function.
- RANGE by time: pick the interval so each partition is roughly 1-50 GB and the total
  stays in the low hundreds to a few thousand partitions. Daily for very high volume,
  monthly for most apps.
- LIST for a small, stable set of categories (region, tier).
- HASH (`MODULUS 16`) only to spread write and vacuum load when no natural range exists.
  It does not help retention and you cannot easily change the modulus later.
- Sub-partitioning (range by month, then hash) multiplies partition count; justify it.

## Creating a partitioned table

```sql
CREATE TABLE events (
    id          bigint GENERATED ALWAYS AS IDENTITY,
    tenant_id   bigint      NOT NULL,
    occurred_at timestamptz NOT NULL,
    kind        text        NOT NULL,
    payload     jsonb,
    PRIMARY KEY (id, occurred_at)             -- must include the partition key
) PARTITION BY RANGE (occurred_at);

CREATE INDEX events_tenant_occurred_idx ON events (tenant_id, occurred_at);  -- propagates

CREATE TABLE events_2026_10 PARTITION OF events
    FOR VALUES FROM ('2026-10-01') TO ('2026-11-01');
CREATE TABLE events_2026_11 PARTITION OF events
    FOR VALUES FROM ('2026-11-01') TO ('2026-12-01');
```

- Bounds are inclusive FROM, exclusive TO. Use timestamptz bounds and be explicit
  about the zone: `'2026-11-01 00:00+00'`, otherwise the session TimeZone decides.
- Every PRIMARY KEY and UNIQUE constraint must include all partition key columns.
  Global uniqueness of `id` alone is not enforceable; identity/sequence values are
  unique in practice, but foreign keys pointing at `events(id)` are impossible.
  Referencing tables must use `(id, occurred_at)`.
- A DEFAULT partition catches rows outside all ranges. It hides missing-partition bugs
  and makes later `CREATE ... PARTITION OF` scan it for conflicting rows. Prefer no
  default plus monitoring that next period's partition exists; if you keep one, alert
  when it has rows.

## Keeping future partitions ahead

Options, in order of preference:

1. **pg_partman** (extension, available on most managed services): declares the
   parent, interval and premake count, and `run_maintenance()` (via its background
   worker or `pg_cron`) creates and retires partitions. Check the installed major
   version; its setup function arguments changed in 5.x.
2. **A scheduled job** in the app or `pg_cron` that creates partitions for the next N
   periods with `CREATE TABLE IF NOT EXISTS ... PARTITION OF`.

Always keep at least two future periods created, and alert if the newest partition's
upper bound is less than 7 days away. Inserts with no matching partition fail outright:
`no partition of relation "events" found for row`.

## Retention

```sql
-- Instant, no bloat, no long DELETE. Needs PG 14+ and no default partition.
ALTER TABLE events DETACH PARTITION events_2025_09 CONCURRENTLY;
-- archive first if required (pg_dump -t events_2025_09, or COPY to object storage)
DROP TABLE events_2025_09;
```

`DETACH ... CONCURRENTLY` cannot run inside a transaction block. Dropping data is
irreversible: show the user which partitions will be dropped and get confirmation, or
confirm that an approved retention policy already automates it.

## Pruning: prove it

```sql
EXPLAIN (ANALYZE, COSTS OFF)
SELECT count(*) FROM events
WHERE occurred_at >= now() - interval '1 day' AND tenant_id = 42;
```

Look for only the expected partitions in the plan, or `Subplans Removed: N` when
pruning happens at execution time (parameters, `now()`, prepared statements).

Pruning fails when:
- The predicate wraps the key: `date(occurred_at) = ...`, `occurred_at::date`.
- The key is compared to a different type requiring a cast on the column side.
- The query filters only through a join to another table (some cases prune at runtime
  in nested loops, many do not).
- `enable_partition_pruning` is off (it is on by default).

## Indexes on partitioned tables

- An index created on the parent is created on every partition, existing and future.
- `CREATE INDEX CONCURRENTLY` is not supported on the parent. On a live table:

```sql
CREATE INDEX events_kind_idx ON ONLY events (kind);          -- invalid shell on parent
CREATE INDEX CONCURRENTLY events_2026_10_kind_idx ON events_2026_10 (kind);
ALTER INDEX events_kind_idx ATTACH PARTITION events_2026_10_kind_idx;
-- repeat per partition; the parent index becomes valid when all are attached
```

## Converting an existing big table

Do not `INSERT INTO new SELECT * FROM old` during business hours and swap. Online path:

1. Create the new partitioned parent with the same columns.
2. Add to the old table a CHECK constraint matching the range it will cover, including
   NOT NULL on the key (`CHECK (occurred_at IS NOT NULL AND occurred_at < '2026-11-01 00:00+00')`),
   `NOT VALID`, then `VALIDATE CONSTRAINT` (no long lock).
3. In a short transaction with `lock_timeout`: rename old table to `events_legacy`,
   rename the new parent to `events`, and `ATTACH PARTITION events_legacy FOR VALUES FROM (MINVALUE) TO ('2026-11-01')`.
   The valid CHECK lets the attach skip its scan.
4. New partitions take new data from the boundary onward. Optionally split the legacy
   partition later, or let retention age it out.
5. Recreate sequences/identity ownership and foreign keys as needed; test on a copy first.

Coordinate the swap with the application (`$zero-downtime-migrations`), and get the
user's go-ahead before running it in production.

## Gotchas

- Identity columns on partitioned parents behave better from PG 17; on older versions
  test it in a scratch database, or use `bigint DEFAULT nextval('events_id_seq')`.
- Row moves between partitions on UPDATE of the key work (11+) but are a delete plus
  insert: triggers fire accordingly, and concurrent updates can raise serialization errors.
- Too many partitions (tens of thousands) slow planning for every query, and each
  partition holds locks during planning when pruning cannot happen at plan time.
- Logical replication of partitioned tables: set `publish_via_partition_root = true`
  on the publication if the subscriber has a different partition layout.
- `ANALYZE` on the parent gathers parent-level statistics; autovacuum does not analyze
  partitioned parents automatically. Run `ANALYZE events` after large loads.
- For heavy time-series workloads with compression and rollups, compare with
  `$timescaledb` before building it by hand.

## Verification

- [ ] Hot queries' plans touch only expected partitions (or show Subplans Removed)
- [ ] Future partitions exist for at least two periods; alert configured
- [ ] Default partition absent or monitored for rows
- [ ] Retention tested on staging: detach concurrently, archive, drop
- [ ] Unique keys and foreign keys redesigned to include the partition key
- [ ] Partition count and size projected for 2-3 years

## Output template

```markdown
## Partitioning plan: <table>
**Reason:** <retention | maintenance | pruning>, with numbers (size, rows/day)
**Strategy:** RANGE (<column>) by <interval>; expected <n> partitions, <size> each
**Keys:** PK <cols>; unique <cols>; FKs affected <list>
**Creation:** <pg_partman config or job>, premake <n>
**Retention:** <keep period>, archive <where>, drop cadence
**Migration path:** <steps, locks, rollback>
**Pruning evidence:** <query -> partitions scanned>
```
