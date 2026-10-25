package outbox

import (
	"context"
	"errors"
	"runtime"
	"testing"
	"time"

	postgres "github.com/fin-assist/go-postgres"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

type fin244ObservedLease struct {
	*pgxpool.Conn
	pids []uint32
}

func (c *fin244ObservedLease) BeginTx(ctx context.Context, opts pgx.TxOptions) (pgx.Tx, error) {
	tx, err := c.Conn.BeginTx(ctx, opts)
	if err != nil {
		return nil, err
	}
	c.pids = append(c.pids, c.Conn.Conn().PgConn().PID())
	return tx, nil
}

func fin244CloseLease(c *pgxpool.Conn) {
	raw := c.Hijack()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = raw.Close(ctx)
}

func TestIntegrationDispatcherPinsSessionAcrossBothTransactionsAndDelivery(t *testing.T) {
	pool := database(t)
	publish(t, pool, "t", true)
	lease, err := pool.Acquire(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	defer fin244CloseLease(lease)
	session := &fin244ObservedLease{Conn: lease}
	pid := lease.Conn().PgConn().PID()
	var id int64
	if err := pool.QueryRow(t.Context(), `SELECT id FROM outbox_events`).Scan(&id); err != nil {
		t.Fatal(err)
	}
	calls := 0
	d := dispatcher(t, pool, Routes{"t": {"x": func(ctx context.Context, _ Event) error {
		calls++
		if _, ok := postgres.Tx(ctx); ok || lease.Conn().PgConn().TxStatus() != 'I' {
			t.Fatal("remote delivery ran inside a PostgreSQL transaction")
		}
		var idle, held bool
		if err := pool.QueryRow(ctx, `SELECT xact_start IS NULL,EXISTS(SELECT 1 FROM pg_locks WHERE pid=$1 AND locktype='advisory' AND granted) FROM pg_stat_activity WHERE pid=$1`, pid).Scan(&idle, &held); err != nil {
			t.Fatal(err)
		}
		if !idle || !held {
			t.Fatalf("selection COMMIT did not leave an idle locked session: idle=%v lock=%v", idle, held)
		}
		// The actual session lock prevents a different dispatcher from entering RPC.
		other := dispatcher(t, pool, Routes{"t": {"x": func(context.Context, Event) error { t.Fatal("concurrent delivery bypassed session lock"); return nil }}}, time.Now)
		if found, err := other.oneFor(ctx, "x"); found || err != nil {
			t.Fatalf("locked event selected concurrently: %v %v", found, err)
		}
		return nil
	}}}, time.Now)
	lock := false
	if found, err := d.dispatch(t.Context(), session, d.lane(""), &lock); !found || err != nil || !lock {
		t.Fatalf("fanout: found=%v lock=%v err=%v", found, lock, err)
	}
	if len(session.pids) != 2 || session.pids[0] != pid || session.pids[1] != pid {
		t.Fatalf("selection/fanout changed backend: %v want %d", session.pids, pid)
	}
	// The original session lock remains until this lease ends, even across fanout COMMIT.
	var held bool
	if err := pool.QueryRow(t.Context(), `SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=$1 AND locktype='advisory' AND granted)`, pid).Scan(&held); err != nil || !held {
		t.Fatalf("fanout lost its session lock: %v %v", held, err)
	}
	// Reuse this dedicated lease for the target selection and acknowledgement.
	if found, err := d.dispatch(t.Context(), session, d.lane("x"), &lock); !found || err != nil {
		t.Fatalf("delivery: %v %v", found, err)
	}
	if calls != 1 || pending(t, pool) != 0 || session.pids[2] != pid {
		t.Fatalf("delivery/ack changed lease or lost row: calls=%d pids=%v", calls, session.pids)
	}
}

func TestIntegrationDispatcherFanoutDeferredCommitFailureIsAtomic(t *testing.T) {
	pool := database(t)
	publish(t, pool, "t", true)
	if _, err := pool.Exec(t.Context(), `CREATE TABLE fin244_blocker(event_id bigint REFERENCES outbox_events(id) DEFERRABLE INITIALLY DEFERRED); INSERT INTO fin244_blocker(event_id) SELECT id FROM outbox_events`); err != nil {
		t.Fatal(err)
	}
	d := dispatcher(t, pool, Routes{"t": {"x": func(context.Context, Event) error { t.Fatal("failed fanout reached RPC"); return nil }, "y": func(context.Context, Event) error { t.Fatal("failed fanout reached RPC"); return nil }}}, time.Now)
	found, err := d.oneFor(t.Context(), "")
	var pgerr *pgconn.PgError
	if !found || !errors.As(err, &pgerr) || pgerr.Code != "23503" {
		t.Fatalf("deferred COMMIT error missing: found=%v err=%v", found, err)
	}
	var originals, targets int
	if err := pool.QueryRow(t.Context(), `SELECT count(*) FILTER(WHERE target=''),count(*) FILTER(WHERE target<>'') FROM outbox_events`).Scan(&originals, &targets); err != nil {
		t.Fatal(err)
	}
	if originals != 1 || targets != 0 {
		t.Fatalf("fanout leaked partial COMMIT: originals=%d targets=%d", originals, targets)
	}
	var sequence int64
	if err := pool.QueryRow(t.Context(), `SELECT last_value FROM outbox_events_id_seq`).Scan(&sequence); err != nil || sequence != 3 {
		t.Fatalf("fixture failed before both recipient inserts: sequence=%d err=%v", sequence, err)
	}
	assertNoSessionLocks(t, pool)
	if _, err := pool.Exec(t.Context(), `DROP TABLE fin244_blocker`); err != nil {
		t.Fatal(err)
	}
	if found, err := d.oneFor(t.Context(), ""); !found || err != nil || pending(t, pool) != 2 {
		t.Fatalf("fanout could not retry after COMMIT failure: %v %v", found, err)
	}
	assertNoSessionLocks(t, pool)
}

func TestIntegrationDispatcherAbnormalExitClosesLockedSession(t *testing.T) {
	for _, outcome := range []string{"panic", "goexit"} {
		t.Run(outcome, func(t *testing.T) {
			pool := database(t)
			publish(t, pool, "t", true)
			d := dispatcher(t, pool, Routes{"t": {"x": func(context.Context, Event) error {
				if outcome == "panic" {
					panic("delivery panic")
				}
				runtime.Goexit()
				return nil
			}}}, time.Now)
			if _, err := d.oneFor(t.Context(), ""); err != nil {
				t.Fatal(err)
			}
			done := make(chan struct{})
			go func() {
				defer close(done)
				defer func() {
					if p := recover(); (p != nil) != (outcome == "panic") {
						t.Errorf("unexpected panic: %v", p)
					}
				}()
				_, _ = d.oneFor(t.Context(), "x")
			}()
			select {
			case <-done:
			case <-time.After(3 * time.Second):
				t.Fatal("abnormal delivery failed to terminate")
			}
			assertNoSessionLocks(t, pool)
			if pending(t, pool) != 1 || pool.Stat().AcquiredConns() != 0 {
				t.Fatal("abnormal delivery lost durable work or leaked a connection")
			}
			// The next worker can deliver the same durable row.
			d.options.Routes["t"]["x"] = func(context.Context, Event) error { return nil }
			if found, err := d.oneFor(t.Context(), "x"); !found || err != nil || pending(t, pool) != 0 {
				t.Fatalf("retry after abnormal exit: %v %v", found, err)
			}
		})
	}
}

func TestIntegrationDispatcherTransactionAbortPreservesWorkAndLeaseSafety(t *testing.T) {
	for _, phase := range []string{"selection", "partial fanout"} {
		for _, outcome := range []string{"panic", "goexit", "cancel"} {
			t.Run(phase+"/"+outcome, func(t *testing.T) {
				pool := database(t)
				publish(t, pool, "t", true)
				d := dispatcher(t, pool, Routes{"t": {"x": func(context.Context, Event) error { t.Fatal("aborted selection reached RPC"); return nil }, "y": func(context.Context, Event) error { t.Fatal("aborted fanout reached RPC"); return nil }}}, time.Now)
				ctx, cancel := context.WithCancel(t.Context())
				defer cancel()
				calls := 0
				d.options.Now = func() time.Time {
					calls++
					if (phase == "selection" && calls == 1) || (phase == "partial fanout" && calls == 3) {
						switch outcome {
						case "panic":
							panic("clock panic")
						case "goexit":
							runtime.Goexit()
						case "cancel":
							cancel()
						}
					}
					return time.Now()
				}
				done := make(chan struct{})
				go func() {
					defer close(done)
					defer func() {
						if p := recover(); (p != nil) != (outcome == "panic") {
							t.Errorf("unexpected panic: %v", p)
						}
					}()
					if _, err := d.oneFor(ctx, ""); outcome == "cancel" && !errors.Is(err, context.Canceled) {
						t.Errorf("cancellation lost: %v", err)
					}
				}()
				select {
				case <-done:
				case <-time.After(5 * time.Second):
					t.Fatal("transaction callback failed to terminate")
				}
				var originals, targets int
				if err := pool.QueryRow(t.Context(), `SELECT count(*) FILTER(WHERE target=''),count(*) FILTER(WHERE target<>'') FROM outbox_events`).Scan(&originals, &targets); err != nil {
					t.Fatal(err)
				}
				if originals != 1 || targets != 0 || pool.Stat().AcquiredConns() != 0 {
					t.Fatalf("abort lost atomicity or leaked lease: originals=%d targets=%d acquired=%d", originals, targets, pool.Stat().AcquiredConns())
				}
				if phase == "partial fanout" {
					var sequence int64
					if err := pool.QueryRow(t.Context(), `SELECT last_value FROM outbox_events_id_seq`).Scan(&sequence); err != nil || sequence < 2 {
						t.Fatalf("callback aborted before first target insert: sequence=%d err=%v", sequence, err)
					}
				}
				assertNoSessionLocks(t, pool)
				d.options.Now = time.Now
				if found, err := d.oneFor(t.Context(), ""); !found || err != nil || pending(t, pool) != 2 {
					t.Fatalf("retry after callback abort: %v %v", found, err)
				}
				assertNoSessionLocks(t, pool)
			})
		}
	}
}
