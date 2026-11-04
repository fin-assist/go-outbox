# Migration tests

For Plainfin, apply [the shared project policy](../../qa-project-context/references/plainfin-policy.md)
and the repository's migration documentation. These scenarios do not authorize
running a live migration, resetting a database or changing deployment policy.

## Real runner and isolated target

Use the declared migration sources, ordering and metadata tables through the
existing runner. Go repositories already using `go-migrate/migratetest` should
extend its actual tests/API, not invent a new wrapper. Provision an owned test
database with the existing harness. Never DROP/CREATE a database name interpolated
from an unvalidated environment variable or default to an unrelated local server.
Identifiers require the driver's quoting, not SQL value placeholders or shell eval.
Use connection details privately; do not echo credentials into commands or logs.

## Fresh and upgrade paths

1. Apply the real migrations to a fresh isolated database. Query the catalog for
   intended columns, types, nullability, defaults, keys and indexes; preserve the
   agreed identifier widths and integer minor-unit money representation.
2. For an upgrade, start at a supported prior migration revision. Insert synthetic
   rows including representative NULLs, duplicate candidates, both users and
   boundary amounts through the appropriate existing fixtures.
3. Apply the migration under test with the runner's genuine targeting mechanism.
   If the tool has no target option, use an isolated staged source containing the
   actual applicable migrations, following repository conventions. Do not invent
   command flags or edit deployed migration history.
4. Assert preserved values and intended changes. Verify old/new writer or reader
   compatibility where the rollout requires it, and refusal paths where relevant.

A successful runner exit alone does not prove data survived. Tests must compare
values, counts, ownership and ordering where these are part of the contract.

## Reversible changes

Use up → down → up through the real supported mechanism in one controlled scenario.
Verify catalog and preserved data after every step. Keep the fixture before the
migration under test so reapplication checks its real state transition; resetting
the whole database can hide a broken down/up transition.

Inspect destructive data loss explicitly. Recreating a dropped column/table restores
shape, not its old contents. A runner metadata flag, manual version-row edit or a
fresh install is not proof of rollback. Do not invent down SQL when the repository
uses a different runner or migration ownership scheme.

## Intentionally irreversible changes

Check the agreed recovery strategy: compatible rollout stages, a tested forward
fix, or restore into an isolated database from a permitted synthetic backup. Bound
the data-loss/recovery expectations and verify resulting data and versions. Do not
force an unsafe inverse transformation or treat an unexecuted restore plan as a
successful restore test. Production backup access and restore drills require their
own task authorization.

## Schema reproduction and drift

Compare the result of committed migrations with the repository's canonical schema
or explicit catalog assertions. If schema dumps are already used, normalize known
irrelevant owner/privilege/order noise before interpreting differences. Look for
unintended changed objects, not exact raw dump bytes alone. Avoid capturing data or
secrets in an artifact.

Prisma/TypeORM/Alembic recipes are applicable only to projects already using those
tools. Use the installed version's genuine migration/revert/diff commands; an ORM
schema-sync operation bypasses the migration path. In particular, Prisma's
`migrate resolve --rolled-back` updates failed-migration metadata rather than
reversing SQL. These recipes do not require adding an ORM to Go repositories.
