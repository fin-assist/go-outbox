package outbox

import (
	"context"
	"errors"
	"runtime"
	"testing"
	"time"

	postgres "github.com/fin-assist/go-postgres"
	"github.com/jackc/pgx/v5/pgconn"
)

func TestAPIPublisherComposesWithServiceManager(t *testing.T) {
	for _, outcome := range []string{"commit", "error", "panic", "cancel", "goexit", "deferred commit failure"} {
		t.Run(outcome, func(t *testing.T) {
			pool := database(t)
			if _, err := pool.Exec(t.Context(), `CREATE TABLE fin244_parent(id bigint PRIMARY KEY); INSERT INTO fin244_parent VALUES(1); CREATE TABLE fin244_business(id bigint PRIMARY KEY,parent_id bigint REFERENCES fin244_parent(id) DEFERRABLE INITIALLY DEFERRED)`); err != nil {
				t.Fatal(err)
			}
			manager := postgres.New(pool)
			publisher, err := NewPublisher(manager, time.Now)
			if err != nil {
				t.Fatal(err)
			}
			ctx, cancel := context.WithCancel(t.Context())
			defer cancel()
			businessErr := errors.New("business refusal")
			done := make(chan struct{})
			var returned error
			go func() {
				defer close(done)
				defer func() {
					if p := recover(); (p != nil) != (outcome == "panic") {
						t.Errorf("unexpected panic: %v", p)
					}
				}()
				returned = manager.Run(ctx, func(ctx context.Context) error {
					tx, ok := manager.Tx(ctx)
					if !ok {
						return errors.New("service transaction missing")
					}
					if _, err := tx.Exec(ctx, `INSERT INTO fin244_business VALUES(1,1)`); err != nil {
						return err
					}
					if err := manager.Run(ctx, func(ctx context.Context) error {
						return publisher.Publish(ctx, "t", "k", []byte("payload"))
					}); err != nil {
						return err
					}
					// Joining the service transaction must never publish early.
					if pending(t, pool) != 0 {
						t.Error("nested publisher committed before the service")
					}
					switch outcome {
					case "error":
						return businessErr
					case "panic":
						panic("business panic")
					case "cancel":
						cancel()
						return ctx.Err()
					case "goexit":
						runtime.Goexit()
					case "deferred commit failure":
						_, err := tx.Exec(ctx, `DELETE FROM fin244_parent WHERE id=1`)
						return err
					}
					return nil
				})
			}()
			select {
			case <-done:
			case <-time.After(5 * time.Second):
				t.Fatal("service transaction did not finish")
			}
			if outcome == "error" && !errors.Is(returned, businessErr) || outcome == "cancel" && !errors.Is(returned, context.Canceled) {
				t.Fatalf("service error lost: %v", returned)
			}
			if outcome == "deferred commit failure" {
				var pgerr *pgconn.PgError
				if !errors.As(returned, &pgerr) || pgerr.Code != "23503" {
					t.Fatalf("actual COMMIT failure missing: %v", returned)
				}
			}
			var business int
			if err := pool.QueryRow(t.Context(), `SELECT count(*) FROM fin244_business`).Scan(&business); err != nil {
				t.Fatal(err)
			}
			want := map[bool]int{true: 1}[outcome == "commit"]
			if events := pending(t, pool); events != want || business != want || pool.Stat().AcquiredConns() != 0 {
				t.Fatalf("service/outbox atomicity: business=%d events=%d acquired=%d", business, events, pool.Stat().AcquiredConns())
			}
		})
	}
}

func TestAPIPublisherRejectsForeignManagerTransaction(t *testing.T) {
	pool := database(t)
	owner, foreign := postgres.New(pool), postgres.New(pool)
	publisher, err := NewPublisher(owner, time.Now)
	if err != nil {
		t.Fatal(err)
	}
	err = foreign.Run(t.Context(), func(ctx context.Context) error {
		return publisher.Publish(ctx, "t", "k", nil)
	})
	if !errors.Is(err, ErrNoTransaction) || pending(t, pool) != 0 {
		t.Fatalf("foreign transaction published: %v", err)
	}
	// The owner remains usable after rejecting a foreign context.
	if err := owner.Run(t.Context(), func(ctx context.Context) error { return publisher.Publish(ctx, "t", "k", nil) }); err != nil || pending(t, pool) != 1 {
		t.Fatalf("owner could not commit: %v", err)
	}
}

func TestAPIDispatcherRunRejectsServiceTransactionAndRemainsReusable(t *testing.T) {
	pool := database(t)
	manager := postgres.New(pool)
	publisher, err := NewPublisher(manager, time.Now)
	if err != nil {
		t.Fatal(err)
	}
	delivered := make(chan struct{}, 1)
	d := dispatcher(t, pool, Routes{"t": {"x": func(context.Context, Event) error {
		delivered <- struct{}{}
		return nil
	}}}, time.Now)
	var refusal error
	var before, after int64
	if err := manager.Run(t.Context(), func(ctx context.Context) error {
		if err := publisher.Publish(ctx, "t", "k", nil); err != nil {
			return err
		}
		call, cancel := context.WithTimeout(ctx, 50*time.Millisecond)
		defer cancel()
		before = pool.Stat().AcquireCount()
		refusal = d.Run(call)
		after = pool.Stat().AcquireCount()
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	if !errors.Is(refusal, postgres.ErrForeignTransaction) || after != before || d.running.Load() || len(delivered) != 0 || pending(t, pool) != 1 {
		t.Fatalf("transactional Run did work or retained ownership: err=%v acquired=%d->%d running=%v deliveries=%d", refusal, before, after, d.running.Load(), len(delivered))
	}
	// The rejected call must not poison the next service-owned worker lifecycle.
	ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- d.Run(ctx) }()
	select {
	case <-delivered:
	case <-ctx.Done():
		t.Fatal("dispatcher could not start after refusing a transaction context")
	}
	// Wait for the acknowledgement to commit before cancelling the worker.
	for pending(t, pool) != 0 {
		if ctx.Err() != nil {
			t.Fatal("delivered event was not acknowledged")
		}
	}
	cancel()
	if err := <-done; !errors.Is(err, context.Canceled) || d.running.Load() {
		t.Fatalf("reused worker lifecycle did not stop: err=%v running=%v", err, d.running.Load())
	}
	assertNoSessionLocks(t, pool)
}
