# PostgreSQL DDL lock and rewrite matrix

Read this when a migration contains a statement not covered by the quick table in
SKILL.md, or when you need to explain exactly what a statement blocks. Applies to
PostgreSQL 12 and later unless a version is noted.

## Lock levels, weakest to strongest

| Lock | Taken by (examples) | Blocks |
|---|---|---|
| ACCESS SHARE | `SELECT` | Only ACCESS EXCLUSIVE |
| ROW SHARE | `SELECT ... FOR UPDATE/SHARE` | EXCLUSIVE, ACCESS EXCLUSIVE |
| ROW EXCLUSIVE | `INSERT`, `UPDATE`, `DELETE`, `MERGE` | SHARE and stronger |
| SHARE UPDATE EXCLUSIVE | `VACUUM`, `ANALYZE`, `CREATE INDEX CONCURRENTLY`, `VALIDATE CONSTRAINT`, some `ALTER TABLE SET` | Itself and stronger, plus other schema changes. Reads and writes continue |
| SHARE | `CREATE INDEX` (non-concurrent) | All writes |
| SHARE ROW EXCLUSIVE | `ADD FOREIGN KEY`, `CREATE TRIGGER` | All writes |
| EXCLUSIVE | `REFRESH MATERIALIZED VIEW CONCURRENTLY` | Writes and `FOR UPDATE` reads |
| ACCESS EXCLUSIVE | Most `ALTER TABLE`, `DROP`, `TRUNCATE`, `VACUUM FULL`, `CLUSTER`, `REFRESH MATERIALIZED VIEW` | Everything, including plain `SELECT` |

Any lock, even one that blocks nothing currently running, joins the queue. A waiting
ACCESS EXCLUSIVE request blocks every later request that conflicts with it. That is why
`lock_timeout` matters even for "instant" statements.

## Statement reference

| Statement | Lock on table | Rewrite / full scan | Notes |
|---|---|---|---|
| `ADD COLUMN` nullable, no default | ACCESS EXCLUSIVE (brief) | No | |
| `ADD COLUMN ... DEFAULT <constant or stable expr>` | ACCESS EXCLUSIVE (brief) | No (11+) | `now()` is fine: evaluated once |
| `ADD COLUMN ... DEFAULT <volatile expr>` | ACCESS EXCLUSIVE | Rewrite | e.g. `clock_timestamp()`, `random()`, `gen_random_uuid()` |
| `ADD COLUMN ... GENERATED ALWAYS AS (...) STORED` | ACCESS EXCLUSIVE | Rewrite | PG 18 `VIRTUAL` generated columns do not rewrite |
| `ADD COLUMN ... GENERATED ... AS IDENTITY` | ACCESS EXCLUSIVE | Rewrite | Fills every existing row |
| `DROP COLUMN` | ACCESS EXCLUSIVE (brief) | No | Space reclaimed only by later rewrites |
| `ALTER COLUMN TYPE` | ACCESS EXCLUSIVE | Rewrite + index rebuild | Exceptions below |
| `ALTER COLUMN TYPE varchar(n)` -> larger `varchar(m)` or `text` | ACCESS EXCLUSIVE (brief) | No | Binary-coercible, no rewrite |
| `ALTER COLUMN TYPE numeric(p,s)` -> larger `p`, same `s` | ACCESS EXCLUSIVE (brief) | No | |
| `ALTER COLUMN TYPE int` -> `bigint` | ACCESS EXCLUSIVE | Rewrite | Use a new column + backfill + swap |
| `ALTER COLUMN TYPE timestamp` -> `timestamptz` | ACCESS EXCLUSIVE | No rewrite only when `TimeZone` is UTC (12+) | Verify on a copy |
| `ALTER COLUMN SET NOT NULL` | ACCESS EXCLUSIVE | Full scan, unless a valid `CHECK (col IS NOT NULL)` exists | |
| `ALTER COLUMN DROP NOT NULL` | ACCESS EXCLUSIVE (brief) | No | |
| `ALTER COLUMN SET DEFAULT` / `DROP DEFAULT` | ACCESS EXCLUSIVE (brief) | No | |
| `ALTER COLUMN SET STATISTICS` | SHARE UPDATE EXCLUSIVE | No | |
| `ADD CONSTRAINT CHECK` | ACCESS EXCLUSIVE | Full scan | Use `NOT VALID` |
| `ADD CONSTRAINT CHECK ... NOT VALID` | ACCESS EXCLUSIVE (brief) | No | New writes are checked immediately |
| `ADD CONSTRAINT FOREIGN KEY` | SHARE ROW EXCLUSIVE on both tables | Full scan | Use `NOT VALID` |
| `ADD CONSTRAINT FOREIGN KEY ... NOT VALID` | SHARE ROW EXCLUSIVE on both (brief) | No | |
| `VALIDATE CONSTRAINT` | SHARE UPDATE EXCLUSIVE (ROW SHARE on referenced table for FKs) | Full scan | Reads and writes continue |
| `ADD CONSTRAINT UNIQUE / PRIMARY KEY` | ACCESS EXCLUSIVE | Index build under lock | Build index concurrently, then `USING INDEX` |
| `ADD CONSTRAINT ... USING INDEX` | ACCESS EXCLUSIVE (brief) | No (PK also needs columns NOT NULL) | |
| `ADD CONSTRAINT EXCLUDE` | ACCESS EXCLUSIVE | Index build under lock | No concurrent variant; schedule a window on big tables |
| `DROP CONSTRAINT` | ACCESS EXCLUSIVE (brief) | No | |
| `RENAME COLUMN / TABLE / CONSTRAINT` | ACCESS EXCLUSIVE (brief) | No | Breaks running code |
| `CREATE INDEX` | SHARE | Build | Blocks writes for the whole build |
| `CREATE INDEX CONCURRENTLY` | SHARE UPDATE EXCLUSIVE | Two scans | Not in a transaction; leaves INVALID index on failure |
| `DROP INDEX` | ACCESS EXCLUSIVE (brief) | No | |
| `DROP INDEX CONCURRENTLY` | SHARE UPDATE EXCLUSIVE | No | Not in a transaction |
| `REINDEX` | ACCESS EXCLUSIVE on index, SHARE on table | Build | |
| `REINDEX CONCURRENTLY` | SHARE UPDATE EXCLUSIVE | Build | 12+ |
| `ALTER TABLE SET (autovacuum_...)` | SHARE UPDATE EXCLUSIVE | No | |
| `ALTER TABLE SET LOGGED / UNLOGGED` | ACCESS EXCLUSIVE | Rewrite | |
| `ATTACH PARTITION` | SHARE UPDATE EXCLUSIVE on parent, ACCESS EXCLUSIVE on the attached table | Scans attached table unless a matching valid CHECK exists | Also scans the default partition if one exists |
| `DETACH PARTITION` | ACCESS EXCLUSIVE on parent | No | |
| `DETACH PARTITION CONCURRENTLY` | SHARE UPDATE EXCLUSIVE on parent | No | 14+, not in a transaction, no default partition |
| `CREATE TRIGGER` | SHARE ROW EXCLUSIVE | No | |
| `TRUNCATE` | ACCESS EXCLUSIVE | No | Destructive: needs user approval |
| `VACUUM FULL`, `CLUSTER` | ACCESS EXCLUSIVE | Rewrite | Use `pg_repack` for online rebuilds |
| `ALTER TYPE ... ADD VALUE` (enum) | Lock on the type | No | New value unusable until the transaction commits |

## Checking what a statement locks

Run the statement in a transaction on a staging copy and inspect locks from another
session before rolling back:

```sql
BEGIN;
ALTER TABLE orders ADD COLUMN note text;
SELECT locktype, relation::regclass, mode, granted
FROM pg_locks WHERE pid = pg_backend_pid();
ROLLBACK;
```

To see whether a statement rewrites the table, compare `pg_relation_filenode('orders')`
before and after on a copy: a new filenode means a rewrite.
