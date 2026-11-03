---
name: outbox-pattern
description: Implements the transactional outbox and inbox patterns so database changes and published messages never diverge - outbox table written in the same transaction, a polling relay with FOR UPDATE SKIP LOCKED or CDC with the Debezium outbox event router, per-aggregate ordering, cleanup, and an inbox table for idempotent consumers. Use when code writes to a database and then publishes to Kafka, RabbitMQ, SQS or webhooks, when events are lost or sent for rolled-back data, or when designing reliable messaging between services.
license: MIT
---

# Transactional outbox and inbox

Done well: a message is published if and only if the business transaction committed, at
least once, in per-aggregate order; consumers apply each message exactly once in effect;
and the outbox stays small, monitored and replayable.

## The bug this fixes

```python
# Dual write: two systems, no atomicity
db.save(order)              # commits
broker.publish(order_placed) # process dies here -> event lost forever
```

Reversing the order is no better: publish then crash before commit announces an order
that does not exist. Retrying the publish in a `finally` does not survive a crash.
Search for this shape in the codebase first:

```bash
rg -n "publish|send_message|producer\.send|sns\.publish|sqs\.send_message|channel\.basic_publish" -g '!*test*' | head -40
```

Any publish next to a database write, outside the database transaction, is a candidate.

## The pattern

```text
service transaction:  UPDATE orders ...; INSERT INTO outbox (...)   -- one COMMIT
relay (separate):     read outbox -> publish to broker -> mark published
consumer:             INSERT INTO inbox (message_id) ON CONFLICT DO NOTHING; apply; COMMIT
```

## Outbox table (PostgreSQL)

```sql
CREATE TABLE outbox (
  id             uuid        PRIMARY KEY,               -- also the message id consumers dedupe on
  aggregate_type text        NOT NULL,                  -- 'order' -> routes to a topic
  aggregate_id   text        NOT NULL,                  -- partition/ordering key
  event_type     text        NOT NULL,                  -- 'OrderPlaced'
  payload        jsonb       NOT NULL,
  headers        jsonb       NOT NULL DEFAULT '{}',     -- traceparent, schema version
  created_at     timestamptz NOT NULL DEFAULT now(),
  published_at   timestamptz
);
CREATE INDEX outbox_unpublished ON outbox (created_at) WHERE published_at IS NULL;
```

Write it in the same transaction as the business change:

```python
with db.transaction() as tx:
    tx.execute("UPDATE orders SET status = 'placed' WHERE id = %s", (order.id,))
    tx.execute(
        "INSERT INTO outbox (id, aggregate_type, aggregate_id, event_type, payload, headers) "
        "VALUES (%s, 'order', %s, 'OrderPlaced', %s, %s)",
        (uuid7(), order.id, json.dumps(payload), json.dumps({"traceparent": current_traceparent()})),
    )
```

Use time-ordered IDs (UUIDv7, ULID) so `id` order roughly matches creation order.

## Relay: choose one

| Relay | Pick when | Notes |
|---|---|---|
| Polling publisher | Default; any SQL database; low to moderate volume (hundreds/s) | Simple; adds polling latency (100 ms - 1 s) and DB load |
| CDC with Debezium outbox event router | Kafka already in place; high volume; low latency | Reads the WAL; no polling; needs Kafka Connect and replication slot care |
| Managed CDC (DynamoDB Streams, Firestore triggers, Mongo change streams) | That datastore | Outbox is often the item itself or a sibling item in the same transaction |

### Polling publisher

```sql
-- One batch, inside a transaction held only for the batch
SELECT id, aggregate_type, aggregate_id, event_type, payload, headers
FROM outbox
WHERE published_at IS NULL
ORDER BY created_at, id
LIMIT 100
FOR UPDATE SKIP LOCKED;
-- publish each (await broker acks), then:
UPDATE outbox SET published_at = now() WHERE id = ANY(%(ids)s);
```

- Publish, wait for the broker acknowledgement, then mark published. A crash between the
  two republishes the batch: that is the at-least-once guarantee, handled by the inbox.
- Ordering: `SKIP LOCKED` with several relay workers lets a later event for the same
  aggregate overtake an earlier one. If per-aggregate order matters, run one active relay
  (leader election or an advisory lock: `SELECT pg_try_advisory_lock(42)`), or shard
  workers by `hashtext(aggregate_id) % N`.
- Reduce latency without hammering the DB: `NOTIFY outbox` in the transaction and
  `LISTEN` in the relay as a wake-up signal, with polling as the fallback.
- Set the broker key to `aggregate_id` so ordering holds through the broker partitions.

### Debezium outbox event router (Kafka Connect)

```properties
connector.class=io.debezium.connector.postgresql.PostgresConnector
plugin.name=pgoutput
table.include.list=public.outbox
transforms=outbox
transforms.outbox.type=io.debezium.transforms.outbox.EventRouter
transforms.outbox.table.field.event.id=id
transforms.outbox.table.field.event.key=aggregate_id
transforms.outbox.table.field.event.payload=payload
transforms.outbox.route.by.field=aggregate_type
transforms.outbox.route.topic.replacement=${routedByValue}.events
```

Field names above match the table in this skill; Debezium's defaults are `aggregateid`,
`aggregatetype` and `type`, so map them if you rename. With CDC the relay never updates
rows; delete outbox rows soon after insert (even in the same transaction) since the WAL
already carries them. Monitor replication slot lag: a stalled connector makes Postgres
retain WAL until the disk fills. Connector setup details belong to $cdc-debezium.

## Inbox (consumer side)

```sql
CREATE TABLE inbox (
  consumer    text NOT NULL,
  message_id  uuid NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (consumer, message_id)
);
```

```python
def handle(msg):
    with db.transaction() as tx:
        fresh = tx.execute(
            "INSERT INTO inbox (consumer, message_id) VALUES ('shipping', %s) ON CONFLICT DO NOTHING",
            (msg.id,),
        ).rowcount == 1
        if fresh:
            create_shipment(tx, msg.payload)
    msg.ack()                       # only after commit; a crash before ack redelivers -> deduped
```

When the consumer's effect is naturally idempotent (an upsert keyed by entity with a
version check), the inbox table is optional. When the effect is an external call (email,
payment), pass `msg.id` as the downstream idempotency key as well.

## Housekeeping

- Delete or archive published rows on a schedule (`DELETE ... WHERE published_at < now() - interval '7 days'`
  in batches). Keep enough history to replay recent incidents.
- Prune inbox rows older than the broker's maximum redelivery window.
- Metrics: count and age of the oldest unpublished row (alert when age > 1 minute),
  relay publish errors, consumer DLQ depth.
- Replay: re-publishing is `UPDATE outbox SET published_at = NULL WHERE ...`. Confirm with
  the user before replaying to production consumers; downstream side effects repeat for
  any consumer that is not idempotent.

## Workflow

```
- [ ] 1. Read AGENTS.md; find every dual write (rg above) and the broker client in use
- [ ] 2. Add the outbox migration; write outbox rows inside existing transactions
- [ ] 3. Choose relay (polling by default, Debezium if Kafka Connect exists)
- [ ] 4. Preserve per-aggregate order: single relay or sharded workers, broker key = aggregate_id
- [ ] 5. Add inbox or idempotent handling to every consumer
- [ ] 6. Add cleanup job, oldest-unpublished-age alert and a replay procedure
- [ ] 7. Remove the old direct publish calls
```

## Gotchas

- Publishing inside the transaction (before commit) still announces rolled-back data.
  Only the relay publishes.
- ORMs that flush late can insert the outbox row in a different transaction than the
  aggregate change if you open a new session. Use the same session/unit of work.
- The payload must be built from committed state in the transaction, not re-read later by
  the relay, or it may describe a newer version.
- Large payloads bloat the table and WAL; store a reference to blob storage for big data.
- A relay that marks rows published before the broker acks loses messages on broker
  errors.

## Verification

- Kill the relay between publish and mark: on restart the message is published again and
  consumers apply it once (inbox row count equals distinct message IDs).
- Roll back a business transaction in a test: no outbox row, no message.
- Send several events for one aggregate rapidly with multiple relay workers: consumers see
  them in creation order.
- The oldest-unpublished-age alert fires when the relay is stopped.

## Output contract

```markdown
**Dual writes replaced:** <file:line list>
**Outbox:** migration <path>; written in <n> transactions
**Relay:** polling (single leader via advisory lock) | Debezium EventRouter -> <topics>
**Ordering key:** aggregate_id; broker key set
**Consumers:** inbox table in <services>; idempotency key passed to <external APIs>
**Ops:** cleanup <schedule>, alert on oldest unpublished > 60 s, replay procedure <doc>
```
