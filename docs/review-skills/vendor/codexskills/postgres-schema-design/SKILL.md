---
name: postgres-schema-design
description: Designs PostgreSQL schemas that keep data correct under concurrent writes - column types (bigint identity, uuid, timestamptz, numeric, text), primary and foreign keys with explicit ON DELETE, CHECK, UNIQUE and exclusion constraints, naming conventions, normalization and when to denormalize or use jsonb. Reviews CREATE TABLE statements and ORM models for type and constraint mistakes. Use when creating tables, modeling a new feature, reviewing a schema or migration, or choosing between enum, lookup table and check constraint.
license: MIT
---

# Postgres schema design

A good schema makes invalid data impossible to store, not merely unlikely. Every
business rule the database can check (presence, uniqueness, ranges, relationships)
is a constraint, because application validation is bypassed by scripts, backfills,
races and the next service that writes to the table.

## Before designing

1. Read `AGENTS.md` and existing migrations: naming style, singular vs plural tables,
   id type, timestamp columns, ORM (Prisma, Django, Rails, SQLAlchemy, Drizzle).
   Match the house conventions unless they are actively harmful; flag those separately.
2. Write down the access patterns: the queries this table must serve, expected row
   count in a year, write rate, and who deletes rows. Schema follows usage.
3. Identify the natural key (what makes a row unique in the business sense) even when
   you use a surrogate primary key. It becomes a UNIQUE constraint.

## Type choices

| Data | Use | Avoid |
|---|---|---|
| Surrogate key | `bigint GENERATED ALWAYS AS IDENTITY` | `serial` (legacy, ownership quirks), `int` (overflows at 2.1B) |
| Public or distributed id | `uuid`; generate v7 (time-ordered) with `uuidv7()` in PG 18 or in the app | Random v4 as the clustered-ish PK on huge insert-heavy tables (index locality suffers) |
| Strings | `text`, plus `CHECK (char_length(x) <= n)` when a limit is a real rule | `varchar(n)` as a habit, `char(n)` (pads with spaces) |
| Point in time | `timestamptz` | `timestamp` without time zone for events; it silently drops the offset |
| Calendar date (birthday, due date) | `date` | `timestamptz` at midnight |
| Money | `numeric(19,4)` or integer minor units (`bigint` cents) plus a currency column | `float`/`double`, the `money` type (locale-dependent) |
| Booleans | `boolean NOT NULL DEFAULT false` | Nullable booleans (three states nobody handles) |
| Fixed small set of states | `text` + `CHECK (status IN (...))`, or a lookup table with FK | Native `enum` if values may be removed or reordered later |
| Semi-structured, rarely filtered attributes | `jsonb` | `json` (no indexing, keeps duplicates), `jsonb` for core relational fields |
| Ranges (bookings, validity) | `tstzrange`, `daterange` + exclusion constraint | Two columns with no overlap guarantee |
| IP addresses | `inet` / `cidr` | text |

Enum vs check vs lookup table: native enums are compact but values cannot be dropped
without recreating the type, and `ALTER TYPE ... ADD VALUE` cannot be used in the same
transaction that adds it. A CHECK constraint is easiest to change. Choose a lookup table
when states carry attributes (label, sort order, is_terminal) or are edited by users.

## Constraints checklist

```sql
CREATE TABLE invoices (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id     bigint      NOT NULL REFERENCES tenants (id) ON DELETE RESTRICT,
    customer_id   bigint      NOT NULL REFERENCES customers (id) ON DELETE RESTRICT,
    number        text        NOT NULL,
    status        text        NOT NULL DEFAULT 'draft'
                  CHECK (status IN ('draft', 'issued', 'paid', 'void')),
    currency      char(3)     NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    total_minor   bigint      NOT NULL CHECK (total_minor >= 0),
    issued_at     timestamptz,
    paid_at       timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT invoices_tenant_number_key UNIQUE (tenant_id, number),
    CONSTRAINT invoices_paid_after_issued CHECK (paid_at IS NULL OR paid_at >= issued_at),
    CONSTRAINT invoices_issued_has_date CHECK (status = 'draft' OR issued_at IS NOT NULL)
);
CREATE INDEX invoices_customer_id_idx ON invoices (customer_id);
```

(`char(3)` is acceptable here because ISO currency codes are exactly three letters.)

- `NOT NULL` by default. Each nullable column needs a reason: "unknown" and "not
  applicable" are different, and NULL conflates them.
- Every foreign key has an explicit `ON DELETE`: `RESTRICT` for financial or audit
  data, `CASCADE` only for true ownership (order -> order_items), `SET NULL` when the
  link is optional. Index the referencing column (Postgres does not).
- Uniqueness is scoped: in multi-tenant tables, unique keys include `tenant_id`.
- Unique with nullable columns: NULLs are distinct by default, so two rows with
  `(42, NULL)` both insert. Use `UNIQUE NULLS NOT DISTINCT` (PG 15+) or a partial
  unique index (`WHERE deleted_at IS NULL`) for soft-deleted rows.
- No overlapping bookings:

```sql
CREATE EXTENSION IF NOT EXISTS btree_gist;
ALTER TABLE bookings ADD CONSTRAINT bookings_no_overlap
    EXCLUDE USING gist (room_id WITH =, during WITH &&);
```

- `updated_at` does not maintain itself. Use a trigger or make the ORM own it, and say which.

## Naming

- `snake_case`, lowercase, no quoted identifiers (quoted `"UserId"` must be quoted forever).
- Pick plural or singular tables and stay consistent with the repo.
- Foreign keys: `<referenced_singular>_id` (`customer_id`).
- Name constraints and indexes explicitly: `<table>_<cols>_key` (unique),
  `<table>_<cols>_idx`, `<table>_<cols>_fkey`, `<table>_<rule>` for checks. Default
  generated names differ between environments created in different ways and make
  later migrations brittle.
- Avoid reserved words as names (`user`, `order`, `group`, `end`); `users`, `orders`
  are fine.
- Timestamps end in `_at`, dates in `_on` or `_date`, booleans start with `is_`/`has_`.

## Normalization and when to break it

- Start in third normal form: each fact stored once. Repeated groups (`phone1`,
  `phone2`, `phone3`) become a child table; comma-separated ids become a join table.
- Denormalize only for a measured read path, and make the copy maintainable: a
  generated column, a trigger, or a materialized view refreshed on a schedule. Document
  which column is the source of truth.
- Use `jsonb` for attributes that vary per row and are read whole (provider payloads,
  user preferences). If you filter, join or aggregate on a key, it belongs in a
  column (`$postgres-jsonb`).
- Polymorphic associations (`commentable_type`, `commentable_id`) cannot have a foreign
  key. Prefer separate nullable FK columns plus a CHECK that exactly one is set:
  `CHECK (num_nonnulls(post_id, photo_id) = 1)`.
- Soft delete (`deleted_at`) spreads `WHERE deleted_at IS NULL` everywhere and breaks
  unique constraints. Prefer moving rows to an archive table unless undo is a real
  requirement.

## Multi-tenant tables

Put `tenant_id` on every tenant-owned table, include it in unique keys and as the
leading column of most indexes, and consider composite foreign keys
`(tenant_id, customer_id) REFERENCES customers (tenant_id, id)` so a row can never
point at another tenant's data. For enforcement in the database see
`$postgres-row-level-security`.

## Gotchas

- Changing a column type later usually rewrites the table under an exclusive lock.
  Get `bigint` and `timestamptz` right on day one (`$zero-downtime-migrations`).
- `DEFAULT now()` records transaction start, so all rows in one transaction share it.
  Use `clock_timestamp()` when you need wall-clock time per row.
- Generated columns: `GENERATED ALWAYS AS (...) STORED` must use immutable
  expressions; PG 18 adds `VIRTUAL` (computed on read, the new default when neither is given).
- ORMs default to their own choices (Prisma `DateTime` maps to `timestamp(3)` without
  time zone unless annotated `@db.Timestamptz`; Rails `datetime` maps to `timestamp`
  unless the PostgreSQL adapter's `datetime_type` is set to `:timestamptz`). Check
  the generated SQL, not the model file.
- Column order matters little for correctness, but grouping fixed-width columns
  (bigint, timestamptz) before variable-width ones reduces padding on very large tables.

## Verification

- [ ] Generated DDL inspected (`pg_dump --schema-only -t <table>` or the migration SQL)
- [ ] Every column has a deliberate type and nullability; every FK has ON DELETE and an index
- [ ] Natural keys enforced with UNIQUE, scoped by tenant where relevant
- [ ] Constraint violations tested: insert a row that breaks each CHECK/UNIQUE/FK and
      confirm the database rejects it (a quick SQL script or an integration test)
- [ ] Access-pattern queries run against the schema with EXPLAIN on seeded data

## Output template

```markdown
## Schema: <feature>
**Tables:** <name>: <purpose, expected rows/year, write rate>
**DDL:** <CREATE TABLE statements with named constraints and indexes>
**Rules enforced in DB:** <list each constraint and the business rule it encodes>
**Rules left to the app (and why):** <list>
**Access patterns covered:** <query -> index>
**Open questions:** <deletion policy, retention, tenancy>
```
