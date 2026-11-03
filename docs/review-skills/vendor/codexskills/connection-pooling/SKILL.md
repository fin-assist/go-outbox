---
name: connection-pooling
description: Sizes and configures database connection pools end to end - app-side pools (node-postgres, SQLAlchemy, HikariCP, Go database/sql, Rails), PgBouncer in transaction mode, RDS Proxy and hosted poolers such as Supavisor and Neon - against the server's max_connections budget, including serverless and autoscaled fleets. Use when you see "too many connections" or "remaining connection slots are reserved", pool timeouts, prepared statement errors behind PgBouncer, Lambda or edge functions exhausting Postgres, or when choosing pool sizes before scaling out.
license: MIT
---

# Connection pooling

Done well means: the sum of every pool that can exist at peak fits inside the database's
connection budget with headroom, each process waits briefly for a connection instead of
opening more, and nothing in the app relies on session state the pooler does not keep.

## Inspect before changing anything

1. Read `AGENTS.md` and find every place a connection is created: ORM config,
   `DATABASE_URL` variants, background workers, cron jobs, migration runners, BI tools.
2. Count the fleet at peak, not at rest: replicas x processes per replica x pool size,
   plus autoscaling maximums, deploy overlap (old and new pods alive together) and workers.
3. Read the server limits (needs DB access; ask before connecting to production):

```sql
SHOW max_connections;
SHOW superuser_reserved_connections;
SELECT count(*), state, usename, application_name
FROM pg_stat_activity GROUP BY 2, 3, 4 ORDER BY 1 DESC;
```

4. Note the managed-service ceiling. On RDS and Cloud SQL, `max_connections` derives from
   instance memory; Supabase and Neon publish per-plan limits for direct and pooled access.

## The budget

```
usable = max_connections
       - superuser_reserved_connections
       - admin/migration/replication slack (5-10)
peak   = sum over services of (max replicas x processes x pool max)
         + deploy overlap + workers + cron + humans
rule:  peak <= 0.8 x usable, or put a pooler in front
```

Worked example: `max_connections = 200`, 3 reserved, 10 slack leaves 187 usable. An API
with up to 12 pods x 4 Puma workers x pool 5 = 240 is already over before Sidekiq. Either
drop per-process pools, or route through PgBouncer so 240 clients share about 40 server
connections.

More connections is rarely faster. Postgres runs one backend process per connection; past
roughly `2-4 x CPU cores` of concurrently active queries, throughput flattens and latency
climbs. Size the server side for active work, the client side for concurrency.

## Decide where pooling happens

| Situation | Default | Why |
|---|---|---|
| A few long-lived app processes, fits budget | App-side pool only | Simplest; no extra hop |
| Many processes or replicas, budget exceeded | PgBouncer (transaction mode) between app and DB | Multiplexes thousands of clients onto tens of backends |
| AWS Lambda against RDS/Aurora | RDS Proxy | Managed, IAM auth, survives function bursts |
| Supabase | Supavisor transaction mode (port 6543) for serverless; direct or session mode (5432) for long-lived servers and migrations | Matches each workload's session needs |
| Neon | The `-pooler` hostname for app traffic; direct host for migrations and `pg_dump` | Pooler is PgBouncer in transaction mode |
| Edge runtimes without TCP | Provider HTTP/WebSocket drivers (Neon serverless driver, Turso, Hyperdrive on Cloudflare) | No raw sockets available |
| MySQL with many clients | ProxySQL, or the managed proxy | Same multiplexing idea |

Branching and pooling specifics of Neon and Supabase live in `$serverless-postgres`.

## PgBouncer configuration

```ini
[databases]
app = host=10.0.0.5 port=5432 dbname=app

[pgbouncer]
listen_addr = 0.0.0.0
listen_port = 6432
auth_type = scram-sha-256
auth_file = /etc/pgbouncer/userlist.txt
pool_mode = transaction
max_client_conn = 5000          ; client sockets are cheap
default_pool_size = 20          ; server connections per (db, user) pair
min_pool_size = 5
reserve_pool_size = 5
reserve_pool_timeout = 3
max_db_connections = 60         ; hard cap per database across users
server_lifetime = 3600
server_idle_timeout = 600
query_wait_timeout = 30         ; fail fast instead of queueing forever
max_prepared_statements = 200   ; protocol-level prepared statements (1.21+)
ignore_startup_parameters = extra_float_digits,options
```

- `default_pool_size` applies per database and user pair. Ten app roles x 20 = 200 server
  connections; cap with `max_db_connections` or `max_user_connections`.
- PgBouncer is single-threaded. Past roughly one saturated core, run several instances
  with `so_reuseport = 1` or behind a load balancer.
- Release 1.24 made `max_prepared_statements = 200` the default; older installs default
  to 0 (disabled). Check `pgbouncer --version` before assuming either.

### What breaks in transaction mode

A server connection returns to the pool at `COMMIT`, so the next transaction may run on
a different backend. These features assume a stable session:

| Feature | Symptom | Fix |
|---|---|---|
| `SET search_path` / `SET statement_timeout` at session level | Settings leak to other clients or vanish | `SET LOCAL` inside the transaction, or set on the role: `ALTER ROLE app SET statement_timeout = '5s'` |
| SQL-level `PREPARE` / `EXECUTE` | "prepared statement does not exist" | Use protocol-level prepares with `max_prepared_statements`, or disable prepares in the driver |
| `LISTEN` / `NOTIFY` listeners | Notifications lost | Dedicated direct connection for listeners |
| Session advisory locks (`pg_advisory_lock`) | Lock held by a backend another client now uses | `pg_advisory_xact_lock` |
| Temp tables across statements, `WITH HOLD` cursors | Objects missing | Keep inside one transaction or use a direct connection |

Driver flags for poolers without prepared statement support:

- asyncpg: `create_pool(..., statement_cache_size=0)`
- Prisma: append `?pgbouncer=true` to the pooled URL; give `directUrl` the direct host
  for migrations.
- pgx (Go): `default_query_exec_mode=simple_protocol` or `exec` in the DSN.
- Rails: `prepared_statements: false` in `database.yml` when the pooler cannot track them.

### Admin console

```bash
psql -h 127.0.0.1 -p 6432 -U pgbouncer_admin pgbouncer -c 'SHOW POOLS;'
```

Watch `cl_waiting` (clients queued) and `maxwait` (seconds the oldest has waited). Sustained
`cl_waiting > 0` with `sv_active` at the pool size means the pool, or the database behind
it, is the bottleneck. `SHOW STATS` gives per-database query and wait totals.

## App-side pool settings

Set a small max, a short acquire timeout, and a lifetime below anything in the path that
kills idle connections (load balancers, NAT gateways, the pooler's `server_lifetime`).

```js
// node-postgres
import pg from "pg";
export const pool = new pg.Pool({
  connectionString: process.env.DATABASE_URL,
  max: 10,
  idleTimeoutMillis: 30_000,
  connectionTimeoutMillis: 5_000,
});
```

```python
# SQLAlchemy 2.x, long-lived process
engine = create_engine(url, pool_size=5, max_overflow=5, pool_timeout=10,
                       pool_recycle=1800, pool_pre_ping=True)
# Behind PgBouncer or in short-lived processes: let the external pooler pool
from sqlalchemy.pool import NullPool
engine = create_engine(pooled_url, poolclass=NullPool)
```

```go
db.SetMaxOpenConns(10)
db.SetMaxIdleConns(10)
db.SetConnMaxLifetime(30 * time.Minute)
db.SetConnMaxIdleTime(5 * time.Minute)
```

- HikariCP: `maximumPoolSize` (fixed size: leave `minimumIdle` unset), `connectionTimeout`
  of a few seconds, `maxLifetime` at least 30 s below any infrastructure idle cutoff.
- Rails: `pool` in `database.yml` should equal the thread count (`RAILS_MAX_THREADS`) of
  that process type; Sidekiq needs `pool >= concurrency`.
- Thread pools and connection pools interact: a pool smaller than the worker threads that
  need it causes timeouts; a much larger pool only wastes budget.

## Serverless and autoscaled fleets

- Every function instance holds its own pool, so pool `max` should be 1-2 per instance,
  and concurrency limits on the function become your connection ceiling.
- Create the client outside the handler so warm invocations reuse it; never `end()` the
  pool at the end of each request.
- Set reserved or maximum concurrency on Lambda (and max instances on Cloud Run, Vercel)
  to keep a burst from opening thousands of connections.
- Vercel Fluid compute: recent `@vercel/functions` exposes `attachDatabasePool(pool)` to
  close idle connections before an instance is suspended; check the installed version.
- Prisma in serverless: `connection_limit=1` on the pooled URL, plus the pooler.

## Verification

- [ ] Peak connection arithmetic written down and under 80% of usable
- [ ] `SELECT count(*) FROM pg_stat_activity` during a load test stays flat as clients grow
- [ ] PgBouncer `SHOW POOLS`: `cl_waiting` returns to 0 between bursts
- [ ] Pool acquire timeouts appear in app logs as a distinct error, not a generic 500
- [ ] Migrations and `pg_dump` use the direct URL, app traffic the pooled one
- [ ] A rolling deploy (old and new pods together) stays under budget

If acquire timeouts persist while the database is idle, the pool is too small for the
thread count. If they persist while `pg_stat_activity` shows many `active` queries, the
database is saturated: fix slow queries (`$postgres-query-tuning`) instead of raising limits.

## Gotchas

- Raising `max_connections` costs memory per backend (`work_mem` is per operation, per
  backend) and requires a restart. It hides the problem and rarely helps throughput.
- `idle in transaction` sessions pin connections and block vacuum. Set
  `idle_in_transaction_session_timeout` on the role (for example `'60s'`).
- Health checks that open a new connection each probe consume slots; reuse the pool.
- Migration tools that take advisory locks or run DDL in session state need the direct
  connection, never the transaction-mode pooler.
- Read replicas need their own budget; replica `max_connections` must be at least the
  primary's or the standby refuses to start.

## Output template

```markdown
**Fleet at peak:** api 12 pods x 4 workers x pool 5 (240), sidekiq 4 x 10 (40), cron 5
**Server budget:** max_connections 200, usable 187
**Change:** PgBouncer transaction mode, default_pool_size 25, max_db_connections 60;
app pools unchanged (clients 285 -> server 60)
**App changes:** session SET -> SET LOCAL in tenant middleware; LISTEN worker on direct URL
**Verified:** load test 3x traffic, pg_stat_activity peak 58, cl_waiting max 0.4 s
**Risks:** prepared statements rely on PgBouncer >= 1.21 (installed 1.24.1)
```
