// Package outbox implements a PostgreSQL transactional outbox with configurable gRPC
// recipients. Publish uses the service-owned transaction; Run delivers outside it.
// Delivery is at least once and unordered. Recipients must atomically check versions
// and tombstones. Use only a primary pool and apply Migrations before starting Run.
package outbox
