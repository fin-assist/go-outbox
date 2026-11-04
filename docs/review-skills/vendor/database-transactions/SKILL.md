---
name: database-transactions
description: Gets transactional correctness right in PostgreSQL, MySQL InnoDB and SQLite - choosing isolation levels (READ COMMITTED, REPEATABLE READ, SERIALIZABLE), preventing lost updates and write skew with atomic updates, SELECT FOR UPDATE, SKIP LOCKED queues, optimistic version columns and advisory locks, diagnosing lock waits and deadlocks, and retrying serialization failures safely. Use when money, inventory, counters or bookings can race, when deadlock or lock-wait errors appear, when choosing an isolation level, or when reviewing code that reads then writes in separate statements.
license: MIT
---

# Database transactions

Correct concurrent code states which anomaly it prevents and how: an atomic statement,
a row lock, a constraint, or an isolation level with retries. Done well means the race
was reproduced (or reasoned through with two interleaved sessions), transactions stay
short and never wait on the network, and retries are bounded and safe to repeat.

## Before changing code

1. Read `AGENTS.md`, find the database and driver, the ORM's transaction API, and the
   default isolation level actually in use:

```sql
SHOW transaction_isolation;                 -- Postgres (default: read committed)
SELECT @@transaction_isolation;             -- MySQL (default: REPEATABLE-READ)
```

2. Write the invariant in one sentence: "stock never goes below zero", "a room has at
   most one booking per night", "a user is charged once per order".
3. Find every code path that writes the involved rows, including jobs and admin tools.

## Isolation levels in practice

| Level | Postgres | MySQL InnoDB | Prevents | Still allows |
|---|---|---|---|---|
| READ COMMITTED | Default. Each statement sees a fresh snapshot | Available | Dirty reads | Lost updates (read-modify-write), write skew, non-repeatable reads |
| REPEATABLE READ | Snapshot isolation; concurrent update of the same row raises error 40001 | Default. Consistent reads from a snapshot; locking reads and writes see latest data and take next-key locks | Non-repeatable reads; in PG also lost updates (as errors) | Write skew (two rows, each transaction checks the other) |
| SERIALIZABLE | SSI: detects dangerous patterns, raises 40001 | Turns plain SELECTs into locking reads | All anomalies | Needs retries; more aborts (PG) or more blocking (MySQL) |

SQLite has one writer at a time, so write transactions are serializable; use
`BEGIN IMMEDIATE` for transactions that will write (`$sqlite-production`).

Default choice: stay at READ COMMITTED (Postgres) and fix specific races with atomic
statements, row locks or constraints. Use SERIALIZABLE for complex invariants spanning
many rows, and only together with a retry loop.

## Patterns, in order of preference

### 1. Let a constraint enforce it

Uniqueness, foreign keys, CHECK and exclusion constraints are race-free. "Check then
insert" in application code is not:

```sql
INSERT INTO memberships (team_id, user_id) VALUES ($1, $2)
ON CONFLICT (team_id, user_id) DO NOTHING;              -- MySQL: INSERT IGNORE / ON DUPLICATE KEY
```

### 2. One atomic statement

```sql
UPDATE products SET stock = stock - $2
WHERE id = $1 AND stock >= $2
RETURNING stock;            -- 0 rows updated => insufficient stock
```

Never read a value into the app, compute, and write it back (`stock = <app value>`).
Under READ COMMITTED two requests both read 5 and both write 4.

### 3. Pessimistic row lock

```sql
BEGIN;
SELECT * FROM accounts WHERE id IN ($1, $2) ORDER BY id FOR UPDATE;  -- lock in a fixed order
-- check balances, write ledger rows, update both accounts
COMMIT;
```

- `FOR UPDATE` blocks other writers and lockers of those rows until commit.
- Postgres `FOR NO KEY UPDATE` is lighter when you will not change the key (does not
  block inserts of rows referencing it via FK).
- `NOWAIT` fails immediately instead of waiting; `SKIP LOCKED` skips locked rows.

### 4. Optimistic concurrency (version column)

```sql
UPDATE documents SET body = $1, version = version + 1
WHERE id = $2 AND version = $3;     -- 0 rows => someone else saved first: reload or report conflict
```

Best for user-edited records where conflicts are rare and holding locks across think
time is impossible. Rails `lock_version`, Django via a filtered `update()`, JPA `@Version`.

### 5. Job queue with SKIP LOCKED

```sql
WITH next AS (
  SELECT id FROM jobs
  WHERE status = 'queued' AND run_at <= now()
  ORDER BY run_at, id
  LIMIT 10
  FOR UPDATE SKIP LOCKED
)
UPDATE jobs SET status = 'running', locked_at = now()
FROM next WHERE jobs.id = next.id
RETURNING jobs.*;
```

Works in Postgres and MySQL 8+. Index `(status, run_at)`. Add a reaper for jobs stuck
in `running` after a crash.

### 6. Advisory locks (Postgres) for non-row resources

```sql
SELECT pg_advisory_xact_lock(hashtext('import:' || $1));   -- released at commit/rollback
```

Prefer the `_xact_` variants; session-level advisory locks leak across pooled
connections (PgBouncer transaction mode). MySQL equivalent: `GET_LOCK(name, timeout)`,
session-scoped. For locks across services see `$distributed-locks`.

### 7. SERIALIZABLE plus retry

When the invariant spans rows that no single lock covers (write skew, such as "at
least one doctor on call"):

```python
import random, time
from psycopg import errors

RETRYABLE = (errors.SerializationFailure, errors.DeadlockDetected)  # 40001, 40P01

def run_serializable(conn, work, attempts=5):
    for i in range(attempts):
        try:
            with conn.transaction():
                conn.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
                return work(conn)
        except RETRYABLE:
            if i == attempts - 1:
                raise
            time.sleep(min(0.05 * 2 ** i, 1.0) * random.random())  # jittered backoff
```

`SET TRANSACTION` must be the first statement in the transaction. Retry the whole
transaction function, including its reads, never just the failed statement.

## Deadlocks and lock waits

Deadlocks come from two transactions locking the same rows in different orders. The
database kills one (Postgres SQLSTATE 40P01; MySQL error 1213). Fixes:

- Lock rows in a consistent order (sort ids; parent before child).
- Keep transactions short; do fewer things per transaction.
- Index foreign keys and WHERE columns of UPDATE/DELETE, so InnoDB does not lock
  ranges of rows it scanned.
- Retry deadlock victims (bounded, jittered), and log each occurrence.

Diagnose live blocking:

```sql
-- Postgres: who blocks whom
SELECT pid, pg_blocking_pids(pid) AS blocked_by, state, wait_event_type,
       now() - xact_start AS xact_age, left(query, 80)
FROM pg_stat_activity WHERE cardinality(pg_blocking_pids(pid)) > 0;
```

```sql
-- MySQL 8
SELECT * FROM sys.innodb_lock_waits\G
SHOW ENGINE INNODB STATUS\G          -- LATEST DETECTED DEADLOCK section
```

Turn on logging so the next one is captured: Postgres `log_lock_waits = on` (reports
waits longer than `deadlock_timeout`, 1 s by default); MySQL `innodb_print_all_deadlocks = ON`.
These are server settings: propose them to the user rather than changing a shared server.

## Hard rules

- Never call external services (HTTP, payment providers, email, queues) inside a
  database transaction. Hold locks for milliseconds, not for a network timeout. Use the
  outbox pattern for side effects that must match the commit (`$outbox-pattern`).
- Set guards: Postgres `idle_in_transaction_session_timeout` and `statement_timeout`
  per role; MySQL `innodb_lock_wait_timeout` (default 50 s is too long for web requests).
- Retried work must be idempotent: a retry after an ambiguous commit failure (connection
  dropped during COMMIT) may run twice. Use idempotency keys for money movement
  (`$idempotency-keys`).
- ORMs: know where the transaction boundary is. Django autocommits per query unless
  inside `transaction.atomic()`; Rails `transaction do`; SQLAlchemy sessions begin
  implicitly and commit on `session.commit()`; Prisma `$transaction([...])` or the
  interactive form with `isolationLevel`. Nested blocks usually become savepoints.
- Savepoints let a failed statement be rolled back without aborting the transaction
  (Postgres aborts the whole transaction on any error otherwise).

## Reproducing a race

Open two `psql` (or `mysql`) sessions and interleave by hand:

```text
A: BEGIN; SELECT stock FROM products WHERE id = 1;     -- 5
B: BEGIN; SELECT stock FROM products WHERE id = 1;     -- 5
A: UPDATE products SET stock = 4 WHERE id = 1; COMMIT;
B: UPDATE products SET stock = 4 WHERE id = 1; COMMIT; -- lost update: should be 3
```

Then repeat with the fix and show the second session blocks, errors, or updates zero
rows. For code, a test that runs the operation from N threads or processes against a
real database (`$testcontainers`) and checks the invariant afterwards.

## Verification

- [ ] Invariant written down; each write path mapped to a mechanism that enforces it
- [ ] Race reproduced with two sessions or a concurrent test, then shown fixed
- [ ] No network calls inside transactions
- [ ] Retries limited to 40001/40P01 (or MySQL 1213/1205), bounded, jittered, whole-transaction
- [ ] Lock ordering consistent; FK and filter columns indexed
- [ ] Timeouts configured for the app role

## Output template

```markdown
## Concurrency review: <operation>
**Invariant:** <sentence>
**Race:** <interleaving that breaks it today>
**Fix:** <constraint / atomic update / FOR UPDATE / version / SERIALIZABLE+retry>, code location
**Isolation level:** <level and why>
**Retries:** <errors retried, attempts, backoff, idempotency>
**Proof:** <two-session transcript or concurrent test name and result>
```
