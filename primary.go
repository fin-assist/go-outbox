package outbox

import (
	"context"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

// The dispatcher consumes connection ownership, transaction and observation operations;
// its public constructor still accepts the service-owned pgxpool unchanged.
type primaryPool interface {
	Acquire(context.Context) (primaryConn, error)
	Query(context.Context, string, ...any) (pgx.Rows, error)
}
type primaryConn interface {
	BeginTx(context.Context, pgx.TxOptions) (pgx.Tx, error)
	Exec(context.Context, string, ...any) (pgconn.CommandTag, error)
	Release()
	closeDetached(context.Context) error
	waitForNotification(context.Context) error
}
type poolAdapter struct{ *pgxpool.Pool }

func (p poolAdapter) Acquire(ctx context.Context) (primaryConn, error) {
	conn, err := p.Pool.Acquire(ctx)
	if err != nil {
		return nil, err
	}
	return pooledConn{conn}, nil
}

type pooledConn struct{ *pgxpool.Conn }

func (c pooledConn) closeDetached(ctx context.Context) error { return c.Hijack().Close(ctx) }
func (c pooledConn) waitForNotification(ctx context.Context) error {
	_, err := c.Conn.Conn().WaitForNotification(ctx)
	return err
}
