package outbox

import (
	"io/fs"
	"testing"

	"github.com/fin-assist/go-outbox/internal/pgtest"
)

func TestMigrationsAreValid(t *testing.T) {
	if err := pgtest.Validate(Migrations); err != nil {
		t.Fatal(err)
	}
	if MigrationsTable != MigrationsName+"_schema_migrations" {
		t.Fatalf("version table %q does not follow <name>_schema_migrations", MigrationsTable)
	}
}

// Every migration of the library applies, rolls back and applies again on an empty
// database, as the service's migration runner would run it.
func TestMigrationsUpDownUp(t *testing.T) {
	if !hasMigrations(t) {
		t.Skip("the library has no migrations yet")
	}
	pgtest.CheckUpDownUp(t, MigrationsTable, Migrations)
}

func hasMigrations(t *testing.T) bool {
	t.Helper()
	ups, err := fs.Glob(Migrations, "*.up.sql")
	if err != nil {
		t.Fatal(err)
	}
	return len(ups) > 0
}
