package outbox

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"strings"
	"testing"
	"time"

	"github.com/fin-assist/go-postgres"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/prometheus/client_golang/prometheus"
	"google.golang.org/grpc"
	health "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/metadata"
	"google.golang.org/protobuf/proto"
)

var errUnitStorage = errors.New("storage failed")

type unitRow struct {
	values []any
	err    error
}

func (r unitRow) Scan(dest ...any) error {
	if r.err != nil {
		return r.err
	}
	for i, v := range r.values {
		switch d := dest[i].(type) {
		case *bool:
			*d = v.(bool)
		case *int64:
			*d = v.(int64)
		case *int:
			*d = v.(int)
		case *float64:
			*d = v.(float64)
		case *string:
			*d = v.(string)
		case *[]byte:
			*d = append([]byte(nil), v.([]byte)...)
		case *time.Time:
			*d = v.(time.Time)
		}
	}
	return nil
}

type unitRows struct {
	pgx.Rows
	records []unitRow
	pos     int
	err     error
	closed  bool
}

func (r *unitRows) Next() bool             { r.pos++; return r.pos <= len(r.records) }
func (r *unitRows) Scan(dest ...any) error { return r.records[r.pos-1].Scan(dest...) }
func (r *unitRows) Close()                 { r.closed = true }
func (r *unitRows) Err() error             { return r.err }

type unitCall struct {
	query string
	args  []any
}
type unitTx struct {
	pgx.Tx
	queryRow              func(string, ...any) pgx.Row
	query                 func(string, ...any) (pgx.Rows, error)
	exec                  func(string, ...any) (pgconn.CommandTag, error)
	commitErr             error
	committed, rolledBack bool
	calls                 []unitCall
}

func (tx *unitTx) QueryRow(_ context.Context, q string, a ...any) pgx.Row {
	return tx.queryRow(q, a...)
}
func (tx *unitTx) Query(_ context.Context, q string, a ...any) (pgx.Rows, error) {
	return tx.query(q, a...)
}
func (tx *unitTx) Exec(_ context.Context, q string, a ...any) (pgconn.CommandTag, error) {
	tx.calls = append(tx.calls, unitCall{q, a})
	if tx.exec != nil {
		return tx.exec(q, a...)
	}
	return pgconn.NewCommandTag("INSERT 0 1"), nil
}
func (tx *unitTx) Commit(context.Context) error   { tx.committed = true; return tx.commitErr }
func (tx *unitTx) Rollback(context.Context) error { tx.rolledBack = true; return nil }

type unitConn struct {
	tx                 *unitTx
	beginErr, execErr  error
	released, detached bool
	calls              []unitCall
	onWait             func()
}

func (c *unitConn) BeginTx(context.Context, pgx.TxOptions) (pgx.Tx, error) { return c.tx, c.beginErr }
func (c *unitConn) Exec(_ context.Context, q string, a ...any) (pgconn.CommandTag, error) {
	c.calls = append(c.calls, unitCall{q, a})
	return pgconn.NewCommandTag("UPDATE 1"), c.execErr
}
func (c *unitConn) Release()                            { c.released = true }
func (c *unitConn) closeDetached(context.Context) error { c.detached = true; return nil }
func (c *unitConn) waitForNotification(context.Context) error {
	if c.onWait != nil {
		c.onWait()
	}
	return nil
}

type unitPool struct {
	acquire func(context.Context) (primaryConn, error)

	conn                 *unitConn
	acquireErr, queryErr error
	rows                 *unitRows
}

func (p *unitPool) Acquire(ctx context.Context) (primaryConn, error) {
	if p.acquire != nil {
		return p.acquire(ctx)
	}
	return p.conn, p.acquireErr
}
func (p *unitPool) Query(context.Context, string, ...any) (pgx.Rows, error) {
	return p.rows, p.queryErr
}
func unitDispatcher(pool primaryPool, routes Routes) *Dispatcher {
	return &Dispatcher{pool: pool, options: Options{Routes: routes, Now: func() time.Time { return time.Unix(1700000000, 0) }, BatchSize: 1, PollInterval: time.Millisecond, Logger: slog.New(slog.NewTextHandler(io.Discard, nil))}, count: prometheus.NewGaugeVec(prometheus.GaugeOpts{Name: "rows"}, []string{"target"}), age: prometheus.NewGaugeVec(prometheus.GaugeOpts{Name: "age"}, []string{"target"}), failures: prometheus.NewCounterVec(prometheus.CounterOpts{Name: "failures"}, []string{"target"})}
}

func TestUnitDispatcherSelectionAndLeaseOwnership(t *testing.T) {
	for _, stage := range []string{"acquire", "begin", "recovery error", "replica", "query", "scan", "rows error", "empty", "lock error", "locked elsewhere", "load", "commit", "delivered", "send error", "ack error", "retry write error", "removed route"} {
		t.Run(stage, func(t *testing.T) {
			now := time.Unix(1700000000, 0)
			tx := &unitTx{}
			conn := &unitConn{tx: tx}
			pool := &unitPool{conn: conn}
			queries := 0
			sends := 0
			tx.queryRow = func(q string, a ...any) pgx.Row {
				switch {
				case strings.Contains(q, "pg_is_in_recovery"):
					if stage == "recovery error" {
						return unitRow{err: errUnitStorage}
					}
					return unitRow{values: []any{stage == "replica"}}
				case strings.Contains(q, "pg_try_advisory_lock"):
					if len(a) != 1 || a[0] != int64(42) {
						t.Fatal("lock not scoped to selected event")
					}
					if stage == "lock error" {
						return unitRow{err: errUnitStorage}
					}
					return unitRow{values: []any{stage != "locked elsewhere"}}
				default:
					if stage == "load" {
						return unitRow{err: errUnitStorage}
					}
					return unitRow{values: []any{"settings", "user", []byte("protobuf"), "assistant", 2, now}}
				}
			}
			tx.query = func(q string, a ...any) (pgx.Rows, error) {
				queries++
				if !strings.Contains(q, "FOR UPDATE SKIP LOCKED") || a[0] != pgx.QueryExecModeExec || a[4] != "assistant" {
					t.Fatal("selection lost lane or row locking")
				}
				if stage == "query" {
					return nil, errUnitStorage
				}
				rows := &unitRows{}
				if stage == "rows error" {
					rows.err = errUnitStorage
				}
				if stage != "empty" && queries == 1 {
					record := unitRow{values: []any{int64(42), now}}
					if stage == "scan" {
						record.err = errUnitStorage
					}
					rows.records = []unitRow{record}
				}
				return rows, nil
			}
			if stage == "acquire" {
				pool.acquireErr = errUnitStorage
			}
			if stage == "begin" {
				conn.beginErr = errUnitStorage
			}
			if stage == "commit" {
				tx.commitErr = errUnitStorage
			}
			if stage == "ack error" || stage == "retry write error" {
				conn.execErr = errUnitStorage
			}
			routes := Routes{"settings": {"assistant": func(ctx context.Context, e Event) error {
				sends++
				if e.Topic != "settings" || e.Key != "user" || string(e.Payload) != "protobuf" {
					t.Fatal("selected event changed")
				}
				deadline, ok := ctx.Deadline()
				if !ok || time.Until(deadline) > 31*time.Second {
					t.Fatal("RPC lacks deadline")
				}
				if stage == "send error" || stage == "retry write error" {
					return errors.New("secret peer")
				}
				return nil
			}}}
			if stage == "removed route" {
				delete(routes["settings"], "assistant")
			}
			d := unitDispatcher(pool, routes)
			found, err := d.oneFor(t.Context(), "assistant")
			success := stage == "delivered" || stage == "removed route" || stage == "empty" || stage == "locked elsewhere"
			if success != (err == nil) {
				t.Fatalf("found=%v err=%v", found, err)
			}
			if stage == "delivered" {
				if !found || sends != 1 || len(conn.calls) != 1 || !strings.HasPrefix(conn.calls[0].query, "DELETE") || conn.calls[0].args[0] != int64(42) {
					t.Fatal("successful RPC was not acknowledged by selected key")
				}
			}
			if stage == "send error" || stage == "retry write error" || stage == "removed route" {
				if !found || len(conn.calls) != 1 || !strings.HasPrefix(conn.calls[0].query, "UPDATE") {
					t.Fatal("failed delivery not scheduled")
				}
				classification := conn.calls[0].args[2]
				want := "delivery_failed"
				if stage == "removed route" {
					want = "route_unconfigured"
				}
				if classification != want {
					t.Fatal("unbounded failure classification")
				}
				due := conn.calls[0].args[1].(time.Time)
				if !due.After(now) || due.After(now.Add(5*time.Minute)) {
					t.Fatal("retry date out of bounds")
				}
			}
			if stage == "acquire" {
				if conn.released || conn.detached {
					t.Fatal("unacquired connection released")
				}
				return
			}
			lockPossible := stage == "lock error" || stage == "load" || stage == "commit" || stage == "delivered" || stage == "send error" || stage == "ack error" || stage == "retry write error" || stage == "removed route"
			if conn.detached != lockPossible || conn.released == lockPossible {
				t.Fatal("session lock could escape pool ownership")
			}
			if stage != "begin" && !tx.committed && !tx.rolledBack {
				t.Fatal("transaction rollback missing")
			}
		})
	}
}

func TestUnitDispatcherFanoutIsAtomic(t *testing.T) {
	for _, stage := range []string{"unknown", "begin", "insert", "delete", "commit", "success"} {
		t.Run(stage, func(t *testing.T) {
			tx := &unitTx{}
			conn := &unitConn{tx: tx}
			if stage == "begin" {
				conn.beginErr = errUnitStorage
			}
			if stage == "commit" {
				tx.commitErr = errUnitStorage
			}
			tx.exec = func(q string, _ ...any) (pgconn.CommandTag, error) {
				if stage == "insert" && strings.HasPrefix(q, "INSERT") || stage == "delete" && strings.HasPrefix(q, "DELETE") {
					return pgconn.CommandTag{}, errUnitStorage
				}
				return pgconn.NewCommandTag("INSERT 0 1"), nil
			}
			routes := Routes{"settings": {"a": func(context.Context, Event) error { return nil }, "b": func(context.Context, Event) error { return nil }}}
			if stage == "unknown" {
				routes = nil
			}
			d := unitDispatcher(&unitPool{conn: conn}, routes)
			r := row{id: 42, event: Event{Topic: "settings", Key: "user", Payload: []byte("payload")}, created: d.options.Now().Add(-time.Hour)}
			err := d.fanout(t.Context(), conn, postgres.New(conn), r)
			if (stage == "success" || stage == "unknown") != (err == nil) {
				t.Fatal(err)
			}
			if stage == "success" {
				if !tx.committed || tx.rolledBack || len(tx.calls) != 3 {
					t.Fatal("fanout not atomic")
				}
				for _, call := range tx.calls[:2] {
					if call.args[0] != "settings" || call.args[1] != "user" || string(call.args[2].([]byte)) != "payload" || call.args[5] != r.created {
						t.Fatal("fanout changed producer event")
					}
				}
			}
			if stage == "insert" || stage == "delete" {
				if tx.committed || !tx.rolledBack {
					t.Fatal("partial fanout committed")
				}
			}
			if stage == "unknown" {
				if len(conn.calls) != 1 || !strings.Contains(conn.calls[0].query, "route_unconfigured") {
					t.Fatal("unknown event lost instead of delayed")
				}
			}
		})
	}
}

func TestUnitDispatcherObservationBoundedAndAtomic(t *testing.T) {
	for _, stage := range []string{"query", "scan", "rows", "success"} {
		t.Run(stage, func(t *testing.T) {
			now := time.Unix(1700000000, 0)
			rows := &unitRows{records: []unitRow{{values: []any{"", float64(2), now.Add(-time.Hour)}}, {values: []any{"private-target", float64(3), now.Add(-2 * time.Hour)}}}}
			pool := &unitPool{rows: rows}
			if stage == "query" {
				pool.queryErr = errUnitStorage
			}
			if stage == "scan" {
				rows.records[0].err = errUnitStorage
			}
			if stage == "rows" {
				rows.err = errUnitStorage
			}
			d := unitDispatcher(pool, Routes{"settings": {"assistant": func(context.Context, Event) error { return nil }}})
			reg := prometheus.NewRegistry()
			if err := reg.Register(d.count); err != nil {
				t.Fatal(err)
			}
			if err := reg.Register(d.age); err != nil {
				t.Fatal(err)
			}
			err := d.observe(t.Context())
			if (stage == "success") != (err == nil) {
				t.Fatal(err)
			}
			families, e := reg.Gather()
			if e != nil {
				t.Fatal(e)
			}
			for _, f := range families {
				for _, m := range f.Metric {
					for _, l := range m.Label {
						if stage == "success" {
							want := map[string]float64{"": 2, "unconfigured": 3}[l.GetValue()]
							if f.GetName() == "age" {
								if l.GetValue() == "" {
									want = 3600
								} else {
									want = 7200
								}
							}
							if m.Gauge.GetValue() != want {
								t.Fatalf("%s %q=%v want %v", f.GetName(), l.GetValue(), m.Gauge.GetValue(), want)
							}
						}
						if l.GetValue() == "private-target" {
							t.Fatal("unconfigured target leaked")
						}
					}
				}
			}
			if stage == "success" && len(families) != 2 {
				t.Fatal("queue snapshot missing")
			}
			if stage != "success" && len(families) != 0 {
				t.Fatal("partial observation published")
			}
		})
	}
}

func TestUnitDispatcherConstructorWithoutNetwork(t *testing.T) {
	config, err := pgxpool.ParseConfig("postgres://unused@127.0.0.1:1/unused?sslmode=disable")
	if err != nil {
		t.Fatal(err)
	}
	config.MaxConns = 8
	config.MinConns = 0
	config.HealthCheckPeriod = time.Hour
	pool, err := pgxpool.NewWithConfig(t.Context(), config)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	for _, stage := range []string{"default", "batch", "interval", "topic", "empty route", "target", "nil delivery", "many topics", "capacity"} {
		t.Run(stage, func(t *testing.T) {
			o := Options{Pool: pool, Now: time.Now, Logger: slog.New(slog.NewTextHandler(io.Discard, nil)), Registerer: prometheus.NewRegistry(), Routes: Routes{"settings": {"assistant": func(context.Context, Event) error { return nil }}}}
			switch stage {
			case "batch":
				o.BatchSize = 51
			case "interval":
				o.PollInterval = -1
			case "topic":
				o.Routes = Routes{"": o.Routes["settings"]}
			case "empty route":
				o.Routes = Routes{"settings": {}}
			case "target":
				o.Routes = Routes{"settings": {"bad\x00": func(context.Context, Event) error { return nil }}}
			case "nil delivery":
				o.Routes["settings"]["assistant"] = nil
			case "many topics":
				for i := 0; i < 65; i++ {
					o.Routes[strings.Repeat("x", i+1)] = o.Routes["settings"]
				}
			case "capacity":
				for i := 0; i < 9; i++ {
					o.Routes["settings"][strings.Repeat("x", i+1)] = func(context.Context, Event) error { return nil }
				}
			}
			d, err := NewDispatcher(o)
			if stage == "default" {
				if err != nil || d.options.BatchSize != 50 || d.options.PollInterval != 3*time.Second {
					t.Fatal("defaults changed", err)
				}
				delete(o.Routes["settings"], "assistant")
				if len(d.targetNames()) != 1 {
					t.Fatal("caller route mutation changed dispatcher")
				}
			} else if err == nil {
				t.Fatal("invalid dispatcher options accepted")
			}
		})
	}
}

func unitIdleConn() *unitConn {
	tx := &unitTx{queryRow: func(string, ...any) pgx.Row { return unitRow{values: []any{false}} }, query: func(string, ...any) (pgx.Rows, error) { return &unitRows{}, nil }}
	return &unitConn{tx: tx}
}
func TestUnitDispatcherLifecycleAndNotificationCleanup(t *testing.T) {
	ctx, cancel := context.WithCancel(t.Context())
	cancel()
	pool := &unitPool{conn: unitIdleConn(), rows: &unitRows{}}
	d := unitDispatcher(pool, Routes{"settings": {"assistant": func(context.Context, Event) error { return nil }}})
	if err := d.Run(ctx); !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	if d.running.Load() {
		t.Fatal("cancelled run retained ownership")
	}
	d.running.Store(true)
	if err := d.Run(ctx); err == nil {
		t.Fatal("concurrent run admitted")
	}
	d.running.Store(false)
	d.targetLoop(ctx, "assistant", make(chan struct{}))
	d.observeLoop(ctx)
	for _, stage := range []string{"acquire", "listen", "wait"} {
		t.Run(stage, func(t *testing.T) {
			c := unitIdleConn()
			p := &unitPool{conn: c}
			d := unitDispatcher(p, Routes{})
			ctx, cancel := context.WithCancel(t.Context())
			defer cancel()
			if stage == "acquire" {
				p.acquireErr = errUnitStorage
				cancel()
			}
			if stage == "listen" {
				c.execErr = errUnitStorage
				cancel()
			}
			c.onWait = cancel
			if stage == "wait" {
				acquires := 0
				p.acquire = func(context.Context) (primaryConn, error) {
					acquires++
					if acquires == 1 {
						return c, nil
					}
					return unitIdleConn(), nil
				}
			}
			d.wait(ctx, nil)
			if stage == "acquire" {
				if c.detached {
					t.Fatal("unacquired connection detached")
				}
			} else if !c.detached || c.released {
				t.Fatal("LISTEN session returned to pool")
			}
		})
	}
}

func TestUnitDispatcherBatchWorkAndRetry(t *testing.T) {
	for _, stage := range []string{"empty", "storage error", "delivered", "retry"} {
		t.Run(stage, func(t *testing.T) {
			c := unitIdleConn()
			p := &unitPool{conn: c}
			if stage == "storage error" {
				p.acquireErr = errUnitStorage
			}
			if stage == "delivered" || stage == "retry" {
				c.tx.query = func(string, ...any) (pgx.Rows, error) {
					return &unitRows{records: []unitRow{{values: []any{int64(7), time.Unix(1700000000, 0)}}}}, nil
				}
				c.tx.queryRow = func(q string, _ ...any) pgx.Row {
					if strings.Contains(q, "pg_is_in_recovery") {
						return unitRow{values: []any{false}}
					}
					if strings.Contains(q, "pg_try_advisory_lock") {
						return unitRow{values: []any{true}}
					}
					return unitRow{values: []any{"settings", "user", []byte{}, "assistant", 0, time.Unix(1700000000, 0)}}
				}
			}
			d := unitDispatcher(p, Routes{"settings": {"assistant": func(context.Context, Event) error {
				if stage == "retry" {
					return errUnitStorage
				}
				return nil
			}}})
			wakes := 0
			more, err := d.batch(t.Context(), "assistant", func() { wakes++ })
			if stage == "storage error" {
				if err == nil {
					t.Fatal("storage error ignored")
				}
			} else if err != nil {
				t.Fatal(err)
			}
			if more != (stage == "delivered") || wakes != map[bool]int{true: 1, false: 0}[stage == "delivered"] {
				t.Fatal("batch continuation or work notification incorrect")
			}
		})
	}
}

type unitRPCConn struct {
	invoke func(context.Context, string, any, any) error
}

func (c unitRPCConn) Invoke(ctx context.Context, m string, req, res any, _ ...grpc.CallOption) error {
	return c.invoke(ctx, m, req, res)
}
func (unitRPCConn) NewStream(context.Context, *grpc.StreamDesc, string, ...grpc.CallOption) (grpc.ClientStream, error) {
	return nil, errors.New("unused stream")
}
func TestUnitGRPCDeliveryIsolationAndFailures(t *testing.T) {
	calls := 0
	conn := unitRPCConn{invoke: func(ctx context.Context, m string, request, reply any) error {
		calls++
		md, _ := metadata.FromOutgoingContext(ctx)
		if len(md.Get("authorization")) != 0 || len(md.Get("x-client-version")) != 0 || len(md.Get("x-service-authorization")) != 1 || md.Get("x-service-authorization")[0] != "Bearer service" {
			t.Fatal("delivery forwarded user metadata or lost service credential")
		}
		if m != health.Health_Check_FullMethodName || request.(*health.HealthCheckRequest).GetService() != "user" {
			t.Fatal("delivery method or protobuf changed")
		}
		return nil
	}}
	request := func() proto.Message { return &health.HealthCheckRequest{} }
	reply := func() proto.Message { return &health.HealthCheckResponse{} }
	data, err := proto.Marshal(&health.HealthCheckRequest{Service: "user"})
	if err != nil {
		t.Fatal(err)
	}
	source := tokenSource{value: "service"}
	send, err := GRPCDelivery(conn, source, health.Health_Check_FullMethodName, request, reply)
	if err != nil {
		t.Fatal(err)
	}
	ctx := metadata.NewOutgoingContext(t.Context(), metadata.Pairs("authorization", "secret-user", "x-client-version", "private"))
	if err := send(ctx, Event{Payload: data}); err != nil || calls != 1 {
		t.Fatal(err)
	}
	if err := send(ctx, Event{Payload: []byte{255}}); err == nil || calls != 1 {
		t.Fatal("malformed protobuf delivered")
	}
	for _, source := range []tokenSource{{}, {value: "bad\ntoken"}, {err: errors.New("secret")}} {
		send, err := GRPCDelivery(conn, source, health.Health_Check_FullMethodName, request, reply)
		if err != nil {
			t.Fatal(err)
		}
		if err := send(ctx, Event{Payload: data}); err == nil || strings.Contains(err.Error(), "secret") {
			t.Fatal("credential failure leaked", err)
		}
	}
	send, err = GRPCDelivery(conn, source, health.Health_Check_FullMethodName, func() proto.Message { return nil }, reply)
	if err != nil {
		t.Fatal(err)
	}
	if err := send(ctx, Event{Payload: data}); err == nil {
		t.Fatal("nil message delivered")
	}
	conn.invoke = func(context.Context, string, any, any) error { return errors.New("secret dialogue") }
	send, err = GRPCDelivery(conn, source, health.Health_Check_FullMethodName, request, reply)
	if err != nil {
		t.Fatal(err)
	}
	if err := send(ctx, Event{Payload: data}); err == nil || strings.Contains(err.Error(), "secret") {
		t.Fatal("peer error leaked", err)
	}
	for _, method := range []string{"bad", "/bad"} {
		if _, err := GRPCDelivery(conn, source, method, request, reply); err == nil {
			t.Fatal("invalid method accepted")
		}
	}
}

func TestUnitDispatcherRemovedRoutesPassCadence(t *testing.T) {
	now := time.Unix(1700000000, 0)
	for _, stage := range []string{"not due", "drained", "full", "storage error"} {
		t.Run(stage, func(t *testing.T) {
			c := unitIdleConn()
			p := &unitPool{conn: c}
			queries := 0
			c.tx.query = func(q string, a ...any) (pgx.Rows, error) {
				queries++
				names, _ := a[4].([]string)
				if q != removedCandidates || a[0] != pgx.QueryExecModeExec || len(names) != 1 || names[0] != "assistant" {
					t.Fatalf("removed pass used %q %v", q, a)
				}
				if stage == "full" && queries == 1 {
					return &unitRows{records: []unitRow{{values: []any{int64(7), now}}}}, nil
				}
				return &unitRows{}, nil
			}
			c.tx.queryRow = func(q string, _ ...any) pgx.Row {
				if strings.Contains(q, "pg_try_advisory_lock") {
					return unitRow{values: []any{true}}
				}
				if strings.Contains(q, "SELECT topic") {
					return unitRow{values: []any{"settings", "user", []byte{}, "removed", 0, now}}
				}
				return unitRow{values: []any{false}}
			}
			if stage == "storage error" {
				p.acquireErr = errUnitStorage
			}
			d := unitDispatcher(p, Routes{"settings": {"assistant": func(context.Context, Event) error {
				t.Fatal("removed row delivered to a configured target")
				return nil
			}}})
			due := now
			if stage == "not due" {
				due = now.Add(time.Second)
			}
			next, more := d.removedPass(t.Context(), due)
			switch stage {
			case "not due":
				if more || !next.Equal(due) || queries != 0 {
					t.Fatal("pass ran before due")
				}
			case "drained":
				if more || !next.Equal(now.Add(removedRoutesInterval)) || queries != 1 {
					t.Fatal("drained pass did not sleep for the interval")
				}
			case "full":
				if !more || !next.Equal(due) || len(c.calls) != 1 || !strings.Contains(c.calls[0].args[2].(string), "route_unconfigured") {
					t.Fatal("full pass did not stay due or classify the row")
				}
			case "storage error":
				if more || !next.Equal(due) {
					t.Fatal("failed pass was rescheduled instead of retried")
				}
			}
		})
	}
}
