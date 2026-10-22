-- Durable events and independent deliveries share a queue. Empty target is an event
-- awaiting fan-out. bigint counts lifetime inserts, including deleted retry deliveries.
CREATE TABLE outbox_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    topic text NOT NULL,
    event_key text NOT NULL,
    payload bytea NOT NULL,
    target text NOT NULL,
    attempts integer NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL,
    last_error text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL
);
CREATE INDEX outbox_events_due ON outbox_events (next_attempt_at, id);
