// Package migrations holds the SQL migrations of the library, embedded into the binary of
// every service that uses it. Files: NNN_name.up.sql and NNN_name.down.sql, by the rules of
// docs/migrations.md in fin-assist/go-service-template.
package migrations

import "embed"

// FS contains the migration files at its root. The pattern is "*" rather than "*.sql" so
// that a library without migrations yet (a fresh copy of the template) still builds; the
// service's migration runner ignores non-SQL files.
//
//go:embed *
var FS embed.FS
