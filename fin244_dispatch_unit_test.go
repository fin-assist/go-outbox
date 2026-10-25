package outbox

import (
	"context"
	"errors"
	"runtime"
	"strings"
	"testing"
	"time"

	postgres "github.com/fin-assist/go-postgres"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/prometheus/client_golang/prometheus"
)

type fin244Session struct {
	txs       []*fin244Tx
	begins    int
	beginFail int
	execs     int
	execErr   error
	lastSQL   string
	lastArgs  []any
}

func (s *fin244Session) BeginTx(context.Context, pgx.TxOptions) (pgx.Tx, error) {
	s.begins++
	if s.begins == s.beginFail {
		return nil, errors.New("begin failure")
	}
	return s.txs[s.begins-1], nil
}
func (s *fin244Session) Exec(_ context.Context, query string, args ...any) (pgconn.CommandTag, error) {
	s.execs++
	s.lastSQL, s.lastArgs = query, args
	return pgconn.CommandTag{}, s.execErr
}

type fin244Tx struct {
	pgx.Tx
	target                         string
	failure                        string
	queries, execs, commits, rolls int
	rollbackErr                    error
	rollbackContextErr             error
	boundedRollback                bool
}

func (tx *fin244Tx) Commit(context.Context) error {
	tx.commits++
	if tx.failure == "commit" {
		return errors.New("commit failure")
	}
	return nil
}
func (tx *fin244Tx) Rollback(ctx context.Context) error {
	tx.rolls++
	tx.rollbackContextErr = ctx.Err()
	_, tx.boundedRollback = ctx.Deadline()
	return tx.rollbackErr
}
func (tx *fin244Tx) Exec(ctx context.Context, _ string, _ ...any) (pgconn.CommandTag, error) {
	tx.execs++
	if ctx.Err() != nil {
		return pgconn.CommandTag{}, ctx.Err()
	}
	if (tx.failure == "insert" && tx.execs == 1) || (tx.failure == "delete" && tx.execs == 3) {
		return pgconn.CommandTag{}, errors.New(tx.failure + " failure")
	}
	return pgconn.CommandTag{}, nil
}

type fin244Row func(...any) error

func (r fin244Row) Scan(args ...any) error { return r(args...) }
func (tx *fin244Tx) QueryRow(_ context.Context, query string, _ ...any) pgx.Row {
	return fin244Row(func(args ...any) error {
		switch {
		case strings.Contains(query, "pg_is_in_recovery"):
			if tx.failure == "recovery query" {
				return errors.New(tx.failure)
			}
			*args[0].(*bool) = tx.failure == "replica"
		case strings.Contains(query, "pg_try_advisory_lock"):
			if tx.failure == "advisory" {
				return errors.New(tx.failure)
			}
			*args[0].(*bool) = tx.failure != "locked"
		default:
			if tx.failure == "event scan" {
				return errors.New(tx.failure)
			}
			*args[0].(*string), *args[1].(*string), *args[2].(*[]byte), *args[3].(*string) = "t", "k", []byte("payload"), tx.target
			*args[4].(*int), *args[5].(*time.Time) = 0, time.Unix(1, 0)
		}
		return nil
	})
}
func (tx *fin244Tx) Query(ctx context.Context, _ string, _ ...any) (pgx.Rows, error) {
	tx.queries++
	if ctx.Err() != nil {
		return nil, ctx.Err()
	}
	if tx.failure == "query" {
		return nil, errors.New(tx.failure)
	}
	return &fin244Rows{available: tx.queries == 1 && tx.failure != "empty", failure: tx.failure}, nil
}

type fin244Rows struct {
	pgx.Rows
	available bool
	failure   string
}

func (r *fin244Rows) Next() bool { available := r.available; r.available = false; return available }
func (r *fin244Rows) Close()     {}
func (r *fin244Rows) Err() error {
	if r.failure == "rows" {
		return errors.New(r.failure)
	}
	return nil
}
func (r *fin244Rows) Scan(args ...any) error {
	if r.failure == "candidate scan" {
		return errors.New(r.failure)
	}
	*args[0].(*int64), *args[1].(*time.Time) = 7, time.Unix(1, 0)
	return nil
}
func fin244Dispatcher(send Delivery) *Dispatcher {
	return &Dispatcher{options: Options{Now: func() time.Time { return time.Unix(2, 0) }, Routes: Routes{"t": {"x": send, "y": send}}}, failures: prometheus.NewCounterVec(prometheus.CounterOpts{Name: "fin244_failures", Help: "Test failure classifications."}, []string{"target"})}
}

func TestUnitDispatcherSelectionManagerLifecycle(t *testing.T) {
	for _, failure := range []string{"", "empty", "locked", "begin", "recovery query", "replica", "query", "candidate scan", "rows", "advisory", "event scan", "commit"} {
		t.Run(failure, func(t *testing.T) {
			tx := &fin244Tx{target: "x", failure: failure}
			session := &fin244Session{txs: []*fin244Tx{tx}}
			if failure == "begin" {
				session.beginFail = 1
			}
			calls := 0
			d := fin244Dispatcher(func(ctx context.Context, event Event) error {
				calls++
				if tx.commits != 1 || tx.rolls != 0 || event.Topic != "t" || event.Key != "k" {
					t.Fatal("delivery occurred before successful selection commit")
				}
				if _, ok := postgres.Tx(ctx); ok {
					t.Fatal("delivery inherited a database transaction")
				}
				return nil
			})
			lock := false
			found, err := d.dispatch(t.Context(), session, d.lane("x"), &lock)
			success := failure == ""
			empty := failure == "empty" || failure == "locked"
			if found != success || (err == nil) != (success || empty) || calls != map[bool]int{true: 1}[success] {
				t.Fatalf("found=%v err=%v calls=%d", found, err, calls)
			}
			if failure != "begin" && !success && !empty && (tx.rolls != 1 || tx.rollbackContextErr != nil || !tx.boundedRollback) {
				t.Fatalf("failed selection cleanup: %+v", tx)
			}
			if session.execs != calls {
				t.Fatalf("persist ran without delivery: %d", session.execs)
			}
			wantLock := success || failure == "advisory" || failure == "event scan" || failure == "commit"
			if lock != wantLock {
				t.Fatalf("uncertain lock tracking=%v want %v", lock, wantLock)
			}
		})
	}
}

func TestUnitDispatcherFanoutManagerLifecycle(t *testing.T) {
	for _, failure := range []string{"", "begin", "insert", "delete", "commit", "rollback"} {
		t.Run(failure, func(t *testing.T) {
			selectTx, fanoutTx := &fin244Tx{}, &fin244Tx{failure: failure}
			session := &fin244Session{txs: []*fin244Tx{selectTx, fanoutTx}}
			rollbackErr := errors.New("rollback failure")
			if failure == "begin" {
				session.beginFail = 2
			}
			if failure == "rollback" {
				fanoutTx.failure, fanoutTx.rollbackErr = "insert", rollbackErr
			}
			d := fin244Dispatcher(func(context.Context, Event) error { t.Fatal("fanout called remote peer"); return nil })
			lock := false
			found, err := d.dispatch(t.Context(), session, d.lane(""), &lock)
			if !found || (err == nil) != (failure == "") || !lock || selectTx.commits != 1 || selectTx.rolls != 0 || session.begins != 2 || session.execs != 0 {
				t.Fatalf("fanout lifecycle found=%v err=%v selection=%+v fanout=%+v session=%+v", found, err, selectTx, fanoutTx, session)
			}
			if failure == "" && (fanoutTx.commits != 1 || fanoutTx.rolls != 0 || fanoutTx.execs != 3) {
				t.Fatalf("fanout did not commit the full recipient set: %+v", fanoutTx)
			}
			if failure != "" && failure != "begin" && (fanoutTx.rolls != 1 || fanoutTx.rollbackContextErr != nil || !fanoutTx.boundedRollback) {
				t.Fatalf("failed fanout cleanup: %+v", fanoutTx)
			}
			if failure == "rollback" && !errors.Is(err, rollbackErr) {
				t.Fatalf("rollback error lost: %v", err)
			}
		})
	}
}

func TestUnitDispatcherManagerAbnormalExit(t *testing.T) {
	for _, phase := range []string{"selection", "fanout"} {
		for _, outcome := range []string{"cancel", "panic", "goexit"} {
			t.Run(phase+"/"+outcome, func(t *testing.T) {
				first, second := &fin244Tx{}, &fin244Tx{}
				session := &fin244Session{txs: []*fin244Tx{first, second}}
				d := fin244Dispatcher(func(context.Context, Event) error { t.Fatal("aborted transaction reached delivery"); return nil })
				ctx, cancel := context.WithCancel(t.Context())
				defer cancel()
				clockCalls := 0
				d.options.Now = func() time.Time {
					clockCalls++
					if (phase == "selection" && clockCalls == 1) || (phase == "fanout" && clockCalls == 2) {
						switch outcome {
						case "cancel":
							cancel()
						case "panic":
							panic("clock panic")
						case "goexit":
							runtime.Goexit()
						}
					}
					return time.Unix(2, 0)
				}
				// The fake driver observes the same cancellation a real pgx operation does.
				second.failure = ""
				done := make(chan struct{})
				go func() {
					defer close(done)
					defer func() {
						if p := recover(); (p != nil) != (outcome == "panic") {
							t.Errorf("panic=%v outcome=%s", p, outcome)
						}
					}()
					lock := false
					_, err := d.dispatch(ctx, session, d.lane(""), &lock)
					if outcome == "cancel" && !errors.Is(err, context.Canceled) {
						t.Errorf("cancellation lost: %v", err)
					}
				}()
				select {
				case <-done:
				case <-time.After(3 * time.Second):
					t.Fatal("abnormal callback did not finish")
				}
				aborted := first
				if phase == "fanout" {
					aborted = second
				}
				if aborted.commits != 0 || aborted.rolls != 1 || aborted.rollbackContextErr != nil || !aborted.boundedRollback {
					t.Fatalf("aborted callback did not clean up: %+v", aborted)
				}
			})
		}
	}
}

func TestUnitDispatcherRejectsForeignManagerContext(t *testing.T) {
	outerTx := &fin244Tx{}
	outer := postgres.New(&fin244Session{txs: []*fin244Tx{outerTx}})
	lease := &fin244Session{}
	d := fin244Dispatcher(func(context.Context, Event) error { t.Fatal("foreign context delivered"); return nil })
	lock := false
	err := outer.Run(t.Context(), func(ctx context.Context) error {
		_, err := d.dispatch(ctx, lease, d.lane(""), &lock)
		return err
	})
	if !errors.Is(err, postgres.ErrForeignTransaction) || lease.begins != 0 || lease.execs != 0 || lock || outerTx.commits != 0 || outerTx.rolls != 1 {
		t.Fatalf("foreign context touched dispatch session: err=%v session=%+v outer=%+v", err, lease, outerTx)
	}
}

func TestUnitDispatcherPersistsOnlyBoundedDeliveryClassifications(t *testing.T) {
	for _, outcome := range []string{"delivered", "peer failed", "missing target", "unknown topic", "ack failed", "retry persist failed"} {
		t.Run(outcome, func(t *testing.T) {
			tx := &fin244Tx{target: "x"}
			session := &fin244Session{txs: []*fin244Tx{tx}}
			d := fin244Dispatcher(func(context.Context, Event) error {
				if outcome == "peer failed" || outcome == "retry persist failed" {
					return errors.New("PRIVATE financial payload")
				}
				return nil
			})
			if outcome == "missing target" {
				delete(d.options.Routes["t"], "x")
			}
			if outcome == "unknown topic" {
				tx.target = ""
				delete(d.options.Routes, "t")
			}
			persistErr := errors.New("persist failure")
			if outcome == "ack failed" || outcome == "retry persist failed" {
				session.execErr = persistErr
			}
			lock := false
			found, err := d.dispatch(t.Context(), session, d.lane("x"), &lock)
			if !found || !lock || tx.commits != 1 || session.begins != 1 || session.execs != 1 {
				t.Fatalf("delivery lifecycle found=%v err=%v tx=%+v session=%+v", found, err, tx, session)
			}
			if outcome == "peer failed" && !errors.Is(err, errRetryScheduled) {
				t.Fatalf("retry not scheduled: %v", err)
			}
			if session.execErr != nil && !errors.Is(err, persistErr) {
				t.Fatalf("persist error lost: %v", err)
			}
			switch outcome {
			case "peer failed", "retry persist failed", "missing target":
				want := "delivery_failed"
				if outcome == "missing target" {
					want = "route_unconfigured"
				}
				if len(session.lastArgs) != 3 || session.lastArgs[2] != want {
					t.Fatalf("unsafe retry classification: %v", session.lastArgs)
				}
			case "unknown topic":
				if !strings.Contains(session.lastSQL, "last_error='route_unconfigured'") {
					t.Fatal("unknown route classification changed")
				}
			default:
				if !strings.HasPrefix(session.lastSQL, "DELETE") {
					t.Fatal("successful delivery was not acknowledged")
				}
			}
			if err != nil && strings.Contains(err.Error(), "PRIVATE") {
				t.Fatalf("peer error escaped dispatch: %v", err)
			}
		})
	}
}

func TestUnitDispatcherRunRejectsTransactionBeforeClaimingRun(t *testing.T) {
	manager := postgres.New(&fin244Session{txs: []*fin244Tx{{}}})
	// An already-running instance must still reject the transaction context first;
	// a nil pool makes this an entirely network-free check of the entry boundary.
	d := &Dispatcher{}
	d.running.Store(true)
	var refusal error
	if err := manager.Run(t.Context(), func(ctx context.Context) error {
		refusal = d.Run(ctx)
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	if !errors.Is(refusal, postgres.ErrForeignTransaction) || !d.running.Load() {
		t.Fatalf("transaction context reached Run ownership: err=%v running=%v", refusal, d.running.Load())
	}
}
