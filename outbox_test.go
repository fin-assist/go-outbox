package outbox

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

type transaction struct {
	pgx.Tx
	calls int
	fail  int
	cause error
	args  [][]any
}

func (t *transaction) Exec(_ context.Context, _ string, args ...any) (pgconn.CommandTag, error) {
	t.calls++
	t.args = append(t.args, args)
	if t.calls == t.fail {
		if t.cause != nil {
			return pgconn.CommandTag{}, t.cause
		}
		return pgconn.CommandTag{}, errors.New("secret SQL payload")
	}
	return pgconn.CommandTag{}, nil
}

type provider struct{ tx pgx.Tx }

func (p provider) Tx(context.Context) (pgx.Tx, bool) { return p.tx, p.tx != nil }
func TestPublish(t *testing.T) {
	if _, err := NewPublisher(nil, time.Now); err == nil {
		t.Fatal("nil provider accepted")
	}
	if _, err := NewPublisher(provider{}, nil); err == nil {
		t.Fatal("nil clock accepted")
	}
	p, _ := NewPublisher(provider{}, time.Now)
	if !errors.Is(p.Publish(t.Context(), "t", "k", nil), ErrNoTransaction) {
		t.Fatal("missing tx accepted")
	}
	for _, input := range []struct {
		topic, key string
		data       []byte
	}{{"", "k", nil}, {"t", "", nil}, {strings.Repeat("t", 201), "k", nil}, {"t", strings.Repeat("k", 257), nil}, {"t\x00", "k", nil}, {"t", "k", make([]byte, (4<<20)+1)}} {
		if !errors.Is(p.Publish(t.Context(), input.topic, input.key, input.data), ErrInvalidEvent) {
			t.Fatal("invalid event accepted")
		}
	}
	for _, fail := range []int{0, 1, 2} {
		tx := &transaction{fail: fail}
		p, _ = NewPublisher(provider{tx}, func() time.Time { return time.Unix(5, 0) })
		err := p.Publish(t.Context(), "topic", "key", nil)
		if (err != nil) != (fail != 0) {
			t.Fatalf("fail %d: %v", fail, err)
		}
		if err != nil && strings.Contains(err.Error(), "secret") {
			t.Fatal("SQL leaked")
		}
		if tx.args[0][3] != time.Unix(5, 0) {
			t.Fatal("clock ignored")
		}
	}
}
func TestBackoff(t *testing.T) {
	last := time.Duration(0)
	for i := 0; i < 40; i++ {
		for _, j := range []float64{0, 0.5, 0.999999} {
			got := backoff(i, j)
			if got < time.Second || got > 5*time.Minute {
				t.Fatalf("%d: %v", i, got)
			}
		}
		got := backoff(i, 0)
		if got < last {
			t.Fatal("backoff decreased")
		}
		last = got
	}
	if backoff(0, 0) != time.Second || backoff(1, 0) != 2*time.Second || backoff(40, 0) != 5*time.Minute {
		t.Fatal("wrong retry schedule")
	}
}

func TestPublishErrorClassifications(t *testing.T) {
	for _, original := range []error{context.Canceled, context.DeadlineExceeded, &pgconn.PgError{Code: "40001", Message: "secret SQL", Detail: "secret user", InternalQuery: "secret query"}, &pgconn.PgError{Code: "40P01", Message: "secret SQL"}} {
		tx := &transaction{fail: 1, cause: original}
		p, _ := NewPublisher(provider{tx}, time.Now)
		err := p.Publish(t.Context(), "topic", "key", nil)
		if strings.Contains(err.Error(), "secret") {
			t.Fatal("raw error leaked")
		}
		if errors.Is(original, context.Canceled) || errors.Is(original, context.DeadlineExceeded) {
			if !errors.Is(err, original) {
				t.Fatalf("cancellation lost: %v", err)
			}
		} else {
			var safe, expected *pgconn.PgError
			if !errors.As(original, &expected) {
				t.Fatal("expected PostgreSQL error fixture")
			}
			if !errors.As(err, &safe) || safe.Code != expected.Code || safe.Detail != "" || safe.InternalQuery != "" || strings.Contains(safe.Message, "secret") {
				t.Fatalf("unsafe or missing SQLSTATE: %v", safe)
			}
		}
	}
	tx := &transaction{}
	p, _ := NewPublisher(provider{tx}, time.Now)
	for _, names := range [][2]string{{string([]byte{255}), "key"}, {"topic", string([]byte{255})}} {
		if !errors.Is(p.Publish(t.Context(), names[0], names[1], nil), ErrInvalidEvent) {
			t.Fatal("invalid UTF8 accepted")
		}
	}
	if tx.calls != 0 {
		t.Fatal("invalid UTF8 touched transaction")
	}
}
