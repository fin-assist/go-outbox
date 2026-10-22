package outbox

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
)

// ErrNoTransaction means Publish was called outside the service transaction.
var ErrNoTransaction = errors.New("outbox: transaction required")

// ErrInvalidEvent means an event exceeds the documented bounds or has an empty topic/key.
var ErrInvalidEvent = errors.New("outbox: invalid event")

// Transactions supplies the service-owned transaction on the primary.
type Transactions interface {
	Tx(context.Context) (pgx.Tx, bool)
}

// Publisher writes events atomically with a service's changes. It is safe for concurrent use
// if Transactions and the supplied clock are safe for concurrent use.
type Publisher struct {
	tx  Transactions
	now func() time.Time
}

// NewPublisher constructs a publisher; dependencies must be non-nil.
func NewPublisher(tx Transactions, now func() time.Time) (*Publisher, error) {
	if tx == nil || now == nil {
		return nil, errors.New("outbox: transaction provider and clock required")
	}
	return &Publisher{tx: tx, now: now}, nil
}

// Publish inserts an event and transactional NOTIFY on the primary; PostgreSQL emits the
// notification only after commit. The caller owns commit/rollback. Topic is 1..200 bytes,
// key 1..256 bytes and protobuf payload at most 4 MiB. ErrNoTransaction and ErrInvalidEvent
// are caller errors. Database failures are returned without their potentially sensitive text.
func (p *Publisher) Publish(ctx context.Context, topic, key string, payload []byte) error {
	if !validName(topic, 200) || !validName(key, 256) || len(payload) > 4<<20 {
		return ErrInvalidEvent
	}
	tx, ok := p.tx.Tx(ctx)
	if !ok || tx == nil {
		return ErrNoTransaction
	}
	if payload == nil {
		payload = []byte{}
	}
	_, err := tx.Exec(ctx, `INSERT INTO outbox_events(topic,event_key,payload,target,next_attempt_at,created_at) VALUES($1,$2,$3,'',$4,$4)`, topic, key, payload, p.now())
	if err != nil {
		return fmt.Errorf("outbox: insert failed")
	}
	if _, err = tx.Exec(ctx, `SELECT pg_notify('finassist_outbox','')`); err != nil {
		return errors.New("outbox: notify failed")
	}
	return nil
}
func validName(s string, n int) bool { return len(s) > 0 && len(s) <= n && !strings.ContainsRune(s, 0) }
