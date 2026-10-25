// Package pgtest selects the PostgreSQL server for the library's tests and runs the checks
// of its migration source through the public go-migrate harness.
//
// Only the choice of the server lives here: TEST_DATABASE_URL or a test container, a skip
// locally and a failure in CI. Database isolation, the file rules and the up → down → up
// walk are github.com/fin-assist/go-migrate (migratetest and migrate.Validate), the same
// code the services run, so a migration that passes here passes the service's runner.
package pgtest

import (
	"context"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"sync"
	"testing"
	"time"

	migrate "github.com/fin-assist/go-migrate"
	"github.com/fin-assist/go-migrate/migratetest"
	tcpostgres "github.com/testcontainers/testcontainers-go/modules/postgres"
)

// DefaultImage is the PostgreSQL image of the test container; override with
// TEST_POSTGRES_IMAGE. The same major version as production (PostgreSQL 18).
const DefaultImage = "postgres:18-alpine"

// IrreversibleMarker starts the down file of a step that cannot be rolled back; the rest
// of the file raises an error, and the up → down → up walk skips its down.
const IrreversibleMarker = migratetest.IrreversibleMarker

var (
	serverOnce sync.Once
	serverDSN  string
	serverErr  error
)

// DSN returns the DSN of a fresh, empty database dropped at the end of the test.
//
// The server is TEST_DATABASE_URL when set (a local PostgreSQL), otherwise a container
// started once per test binary. Without either the test is skipped locally and fails in
// CI (CI is set), so a check cannot pass by silently not running.
func DSN(t testing.TB) string {
	t.Helper()
	serverOnce.Do(func() { serverDSN, serverErr = server() })
	if serverErr != nil {
		if os.Getenv("CI") != "" {
			t.Fatalf("no PostgreSQL for tests: %v", serverErr)
		}
		t.Skipf("no PostgreSQL for tests (set TEST_DATABASE_URL or run Docker): %v", serverErr)
	}
	return migratetest.DSN(t, serverDSN)
}

func server() (string, error) { return selectServer(os.Getenv, runContainer) }

// container is the part of the test container the server selection reads.
type container interface {
	ConnectionString(ctx context.Context, args ...string) (string, error)
}

// starter starts a PostgreSQL container of image.
type starter func(ctx context.Context, image string) (container, error)

func runContainer(ctx context.Context, image string) (container, error) {
	c, err := tcpostgres.Run(ctx, image,
		tcpostgres.WithDatabase("postgres"),
		tcpostgres.WithUsername("postgres"),
		tcpostgres.WithPassword("postgres"),
		tcpostgres.BasicWaitStrategies(),
	)
	if err != nil {
		return nil, err
	}
	// The container lives until the test binary exits; Ryuk removes it.
	return c, nil
}

// selectServer returns the admin DSN of the test server. A failure to read the connection
// string has a fixed message: its cause can carry the DSN.
func selectServer(getenv func(string) string, start starter) (string, error) {
	if dsn := getenv("TEST_DATABASE_URL"); dsn != "" {
		return dsn, nil
	}
	img := getenv("TEST_POSTGRES_IMAGE")
	if img == "" {
		img = DefaultImage
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	c, err := start(ctx, img)
	if err != nil {
		// No DSN exists yet and the password is the fixed test one, so the cause (Docker
		// down, image pull limit) is kept for diagnostics.
		return "", fmt.Errorf("start test PostgreSQL %s: %w", img, err)
	}
	dsn, err := c.ConnectionString(ctx, "sslmode=disable")
	if err != nil {
		return "", errors.New("read test PostgreSQL connection failed")
	}
	return dsn, nil
}

// Validate checks the files of a migration source by the service runner's rules
// (migrate.Validate): every SQL name matches NNN_name.(up|down).sql, every version has one
// up and one down file, and a version number belongs to one name.
func Validate(fsys fs.FS) error {
	return migrate.Validate(source("migrations", fsys))
}

// CheckUpDownUp validates fsys, then walks a fresh database with the version table table
// through up → down → up of every version (migratetest.CheckUpDownUp). A down file that
// starts with IrreversibleMarker is not run.
func CheckUpDownUp(t testing.TB, table string, fsys fs.FS) {
	t.Helper()
	src := source(table, fsys)
	if err := migrate.Validate(src); err != nil {
		t.Fatal(err)
	}
	migratetest.CheckUpDownUp(t, DSN(t), src)
}

// Apply runs a library migration source with migrate.Up of go-migrate: the same runner
// and isolated version table as a service. Use it for usage tests instead of executing
// embedded SQL directly.
func Apply(t testing.TB, dsn, table string, fsys fs.FS) {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), 2*time.Minute)
	defer cancel()
	if err := migrate.Up(ctx, migrate.DefaultConfig(dsn), []migrate.Source{source(table, fsys)}); err != nil {
		t.Fatal(err)
	}
}

func source(table string, fsys fs.FS) migrate.Source {
	return migrate.Source{Name: table, Table: table, FS: fsys}
}
