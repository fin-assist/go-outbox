package outbox

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/prometheus/client_golang/prometheus"

	"github.com/fin-assist/go-outbox/internal/pgtest"
)

func database(t *testing.T, maxConnections ...int32) *pgxpool.Pool {
	t.Helper()
	dsn := pgtest.DSN(t)
	pgtest.Apply(t, dsn, MigrationsTable, Migrations)
	config, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		t.Fatal(err)
	}
	config.MaxConns = 8
	if len(maxConnections) > 0 {
		config.MaxConns = maxConnections[0]
	}
	pool, err := pgxpool.NewWithConfig(t.Context(), config)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(pool.Close)
	return pool
}
func dispatcher(t *testing.T, pool *pgxpool.Pool, routes Routes, now func() time.Time) *Dispatcher {
	t.Helper()
	d, err := NewDispatcher(Options{Pool: pool, Routes: routes, Now: now, Logger: slog.New(slog.NewTextHandler(io.Discard, nil)), Registerer: prometheus.NewRegistry(), PollInterval: 10 * time.Millisecond})
	if err != nil {
		t.Fatal(err)
	}
	return d
}
func publish(t *testing.T, pool *pgxpool.Pool, topic string, commit bool) {
	t.Helper()
	tx, err := pool.Begin(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	p, _ := NewPublisher(provider{tx}, time.Now)
	if err = p.Publish(t.Context(), topic, "user", []byte{}); err != nil {
		t.Fatal(err)
	}
	if commit {
		err = tx.Commit(t.Context())
	} else {
		err = tx.Rollback(t.Context())
	}
	if err != nil {
		t.Fatal(err)
	}
}
func pending(t *testing.T, pool *pgxpool.Pool) int {
	t.Helper()
	var n int
	if err := pool.QueryRow(t.Context(), `SELECT count(*) FROM outbox_events`).Scan(&n); err != nil {
		t.Fatal(err)
	}
	return n
}
func TestIntegrationTransactionalPersistenceAndFanout(t *testing.T) {
	pool := database(t)
	publish(t, pool, "settings", false)
	if pending(t, pool) != 0 {
		t.Fatal("rollback left event")
	}
	publish(t, pool, "settings", true)
	var a, b int
	d := dispatcher(t, pool, Routes{"settings": {"a": func(context.Context, Event) error { a++; return nil }, "b": func(context.Context, Event) error { b++; return nil }}}, time.Now)
	// A new dispatcher sees the committed event after the publisher/process is gone.
	if err := d.drainOnce(t.Context()); err != nil {
		t.Fatal(err)
	}
	if a != 1 || b != 1 || pending(t, pool) != 0 {
		t.Fatalf("fanout %d %d", a, b)
	}
}
func TestIntegrationIndependentRetryAndUnknownRoutes(t *testing.T) {
	pool := database(t)
	now := time.Now().Add(time.Second)
	var mu sync.Mutex
	clock := func() time.Time { mu.Lock(); defer mu.Unlock(); return now }
	var a, b int
	d := dispatcher(t, pool, Routes{"settings": {"a": func(context.Context, Event) error { a++; return nil }, "b": func(context.Context, Event) error {
		b++
		if b == 1 {
			return errors.New("secret account")
		}
		return nil
	}}}, clock)
	publish(t, pool, "settings", true)
	publish(t, pool, "unknown", true)
	if err := d.drainOnce(t.Context()); err != nil {
		t.Fatal(err)
	}
	if a != 1 || b != 1 || pending(t, pool) != 2 {
		t.Fatalf("independent delivery a=%d b=%d", a, b)
	}
	var attempts int
	var last string
	var due time.Time
	if err := pool.QueryRow(t.Context(), `SELECT attempts,last_error,next_attempt_at FROM outbox_events WHERE target='b'`).Scan(&attempts, &last, &due); err != nil {
		t.Fatal(err)
	}
	if attempts != 1 || last != "delivery_failed" || due.Before(now.Add(time.Second)) || due.After(now.Add(2*time.Second)) {
		t.Fatalf("retry %d %s %v", attempts, last, due)
	}
	if err := d.drainOnce(t.Context()); err != nil {
		t.Fatal(err)
	}
	if b != 1 {
		t.Fatal("retried before due")
	}
	mu.Lock()
	now = now.Add(6 * time.Minute)
	mu.Unlock()
	if err := d.drainOnce(t.Context()); err != nil {
		t.Fatal(err)
	}
	if b != 2 || pending(t, pool) != 1 {
		t.Fatal("retry failed or unknown event lost")
	}
	// Unknown target from an older config remains durable and exposes a bounded label.
	_, err := pool.Exec(t.Context(), `UPDATE outbox_events SET target='removed-secret-target',next_attempt_at=$1`, clock())
	if err != nil {
		t.Fatal(err)
	}
	if err = d.drainOnce(t.Context()); err != nil {
		t.Fatal(err)
	}
	if d.label("removed-secret-target") != "unconfigured" || pending(t, pool) != 1 {
		t.Fatal("removed route lost")
	}
}
func TestIntegrationTwoPodsAndCancellationReleaseLock(t *testing.T) {
	pool := database(t)
	entered := make(chan struct{})
	release := make(chan struct{})
	var calls atomic.Int32
	send := func(ctx context.Context, _ Event) error {
		if calls.Add(1) == 1 {
			close(entered)
			select {
			case <-release:
			case <-ctx.Done():
				return ctx.Err()
			}
		}
		return nil
	}
	routes := Routes{"t": {"target": send}}
	a := dispatcher(t, pool, routes, time.Now)
	b := dispatcher(t, pool, routes, time.Now)
	publish(t, pool, "t", true)
	if _, err := a.one(t.Context()); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(t.Context(), 3*time.Second)
	defer cancel()
	done := make(chan error, 1)
	go func() { _, err := a.one(ctx); done <- err }()
	select {
	case <-entered:
	case <-ctx.Done():
		t.Fatal("no delivery")
	}
	if found, err := b.one(ctx); err != nil || found {
		t.Fatalf("second pod claimed in-flight row: %v %v", found, err)
	}
	if calls.Load() != 1 {
		t.Fatal("overlap")
	}
	cancel()
	if err := <-done; err != nil && !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	if pending(t, pool) != 1 {
		t.Fatal("cancel lost row")
	}
	close(release)
	if _, err := b.one(t.Context()); err != nil {
		t.Fatal(err)
	}
	if pending(t, pool) != 0 || calls.Load() != 2 {
		t.Fatal("session lock leaked")
	}
}
func TestIntegrationRunLifecycle(t *testing.T) {
	pool := database(t)
	delivered := make(chan struct{}, 1)
	d := dispatcher(t, pool, Routes{"t": {"x": func(context.Context, Event) error { delivered <- struct{}{}; return nil }}}, time.Now)
	ctx, cancel := context.WithTimeout(t.Context(), 3*time.Second)
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- d.Run(ctx) }()
	publish(t, pool, "t", true)
	select {
	case <-delivered:
	case <-ctx.Done():
		t.Fatal("Run did not deliver")
	}
	if err := d.Run(ctx); err == nil {
		t.Fatal("concurrent Run accepted")
	}
	cancel()
	if err := <-done; !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
}
func TestIntegrationNotificationWaitCannotUseDrain(t *testing.T) {
	pool := database(t)
	entered := make(chan struct{})
	var once sync.Once
	clock := func() time.Time { once.Do(func() { close(entered) }); return time.Unix(0, 0) }
	d := dispatcher(t, pool, Routes{"t": {"x": func(context.Context, Event) error { return nil }}}, clock)
	d.options.PollInterval = time.Hour
	ctx, cancel := context.WithTimeout(t.Context(), 3*time.Second)
	defer cancel()
	done := make(chan struct{})
	go func() { d.wait(ctx, nil); close(done) }()
	select {
	case <-entered:
	case <-ctx.Done():
		t.Fatal("post-LISTEN drain not entered")
	}
	publish(t, pool, "t", true)
	select {
	case <-done:
		if ctx.Err() != nil {
			t.Fatal("wait timed out without notification")
		}
	case <-ctx.Done():
		t.Fatal("notification failed to wake waiter")
	}
	// All drain queries used epoch, so they could not consume the newly committed event.
	var unexpanded int
	if err := pool.QueryRow(t.Context(), `SELECT count(*) FROM outbox_events WHERE target=''`).Scan(&unexpanded); err != nil {
		t.Fatal(err)
	}
	if unexpanded != 1 {
		t.Fatal("drain consumed future event")
	}
}
func TestIntegrationConfiguration(t *testing.T) {
	pool := configurationPool(t)
	base := Options{Pool: pool, Now: time.Now, Logger: slog.New(slog.NewTextHandler(io.Discard, nil)), Registerer: prometheus.NewRegistry(), Routes: Routes{"t": {"x": func(context.Context, Event) error { return nil }}}}
	for _, mutate := range []func(*Options){func(o *Options) { o.Pool = nil }, func(o *Options) { o.Now = nil }, func(o *Options) { o.Logger = nil }, func(o *Options) { o.Registerer = nil }, func(o *Options) { o.Routes = nil }, func(o *Options) { o.PollInterval = -1 }, func(o *Options) { o.BatchSize = 51 }, func(o *Options) { o.Routes = Routes{"": {}} }, func(o *Options) { o.Routes = Routes{"t": {"": nil}} }} {
		o := base
		mutate(&o)
		if _, err := NewDispatcher(o); err == nil {
			t.Fatal("invalid config accepted")
		}
	}
	if _, err := NewDispatcher(base); err != nil {
		t.Fatal(err)
	}
	if _, err := NewDispatcher(base); err == nil {
		t.Fatal("registration conflict ignored")
	}
}

func TestIntegrationReadyRowBeyondLockedPage(t *testing.T) {
	pool := database(t)
	ctx := t.Context()
	lock, err := pool.Acquire(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { raw := lock.Hijack(); _ = raw.Close(context.Background()) }()
	for i := 0; i < 51; i++ {
		var id int64
		if err = pool.QueryRow(ctx, `INSERT INTO outbox_events(topic,event_key,payload,target,next_attempt_at,created_at) VALUES('t','key',''::bytea,'x',$1,$1) RETURNING id`, time.Now().Add(-time.Second)).Scan(&id); err != nil {
			t.Fatal(err)
		}
		if i < 50 {
			if _, err = lock.Exec(ctx, `SELECT pg_advisory_lock(-$1::bigint)`, id); err != nil {
				t.Fatal(err)
			}
		}
	}
	calls := 0
	d := dispatcher(t, pool, Routes{"t": {"x": func(context.Context, Event) error { calls++; return nil }}}, time.Now)
	if found, e := d.one(ctx); e != nil || !found {
		t.Fatalf("next page %v %v", found, e)
	}
	if calls != 1 || pending(t, pool) != 50 {
		t.Fatal("ready row starved")
	}
}
func TestIntegrationDatabaseErrorsAndLostConnection(t *testing.T) {
	t.Run("dropped table", func(t *testing.T) {
		pool := database(t)
		d := dispatcher(t, pool, Routes{"t": {"x": func(context.Context, Event) error { return nil }}}, time.Now)
		if _, err := pool.Exec(t.Context(), `DROP TABLE outbox_events`); err != nil {
			t.Fatal(err)
		}
		if _, err := d.one(t.Context()); err == nil {
			t.Fatal("select error ignored")
		}
		if err := d.observe(t.Context()); err == nil {
			t.Fatal("observe error ignored")
		}
		tx, err := pool.Begin(t.Context())
		if err != nil {
			t.Fatal(err)
		}
		defer func() { _ = tx.Rollback(context.Background()) }()
		p, _ := NewPublisher(provider{tx}, time.Now)
		if err = p.Publish(t.Context(), "t", "key", nil); err == nil {
			t.Fatal("publish database error ignored")
		}
	})
	t.Run("lost session after remote success", func(t *testing.T) {
		pool := database(t)
		calls := 0
		send := func(ctx context.Context, _ Event) error {
			calls++
			if calls == 1 {
				_, err := pool.Exec(ctx, `SELECT pg_terminate_backend(pid) FROM pg_locks WHERE locktype='advisory' AND pid<>pg_backend_pid() AND database=(SELECT oid FROM pg_database WHERE datname=current_database())`)
				return err
			}
			return nil
		}
		d := dispatcher(t, pool, Routes{"t": {"x": send}}, time.Now)
		publish(t, pool, "t", true)
		if _, err := d.one(t.Context()); err != nil {
			t.Fatal(err)
		}
		if _, err := d.one(t.Context()); err == nil {
			t.Fatal("connection loss ignored")
		}
		if pending(t, pool) != 1 {
			t.Fatal("uncertain ack lost row")
		}
		if _, err := d.one(t.Context()); err != nil {
			t.Fatal(err)
		}
		if calls != 2 || pending(t, pool) != 0 {
			t.Fatal("crash recovery failed")
		}
	})
	t.Run("closed pool", func(t *testing.T) {
		pool := database(t)
		d := dispatcher(t, pool, Routes{"t": {"x": func(context.Context, Event) error { return nil }}}, time.Now)
		pool.Close()
		if _, err := d.one(t.Context()); err == nil {
			t.Fatal("acquire error ignored")
		}
		ctx, cancel := context.WithTimeout(t.Context(), time.Millisecond)
		defer cancel()
		d.wait(ctx, nil)
	})
}

func TestIntegrationCommitNotification(t *testing.T) {
	pool := database(t)
	ctx, cancel := context.WithTimeout(t.Context(), time.Second)
	defer cancel()
	c, err := pool.Acquire(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { raw := c.Hijack(); _ = raw.Close(context.Background()) }()
	if _, err = c.Exec(ctx, `LISTEN finassist_outbox`); err != nil {
		t.Fatal(err)
	}
	publish(t, pool, "t", false)
	short, stop := context.WithTimeout(ctx, 20*time.Millisecond)
	_, err = c.Conn().WaitForNotification(short)
	stop()
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("rollback notified: %v", err)
	}
	publish(t, pool, "t", true)
	notification, err := c.Conn().WaitForNotification(ctx)
	if err != nil || notification.Channel != "finassist_outbox" || notification.Payload != "" {
		t.Fatalf("commit notification %v %v", notification, err)
	}
}

func TestIntegrationMetricRegistrationRollback(t *testing.T) {
	registry := prometheus.NewRegistry()
	conflict := prometheus.NewGaugeVec(prometheus.GaugeOpts{Name: "finassist_outbox_oldest_age_seconds", Help: "Age of the oldest pending row by target."}, []string{"target"})
	if err := registry.Register(conflict); err != nil {
		t.Fatal(err)
	}
	_, err := NewDispatcher(Options{Pool: configurationPool(t), Now: time.Now, Logger: slog.New(slog.NewTextHandler(io.Discard, nil)), Registerer: registry, Routes: Routes{"t": {"x": func(context.Context, Event) error { return nil }}}})
	if err == nil {
		t.Fatal("conflict ignored")
	}
	rows := prometheus.NewGaugeVec(prometheus.GaugeOpts{Name: "finassist_outbox_rows", Help: "Pending rows by configured target; unexpanded events use the empty target."}, []string{"target"})
	if err = registry.Register(rows); err != nil {
		t.Fatalf("partial constructor leaked collector: %v", err)
	}
}

func TestIntegrationFanoutRollbackAfterPartialWork(t *testing.T) {
	for _, failure := range []string{"second target insert", "original delete"} {
		t.Run(failure, func(t *testing.T) {
			pool := database(t)
			ctx := t.Context()
			publish(t, pool, "t", true)
			d := dispatcher(t, pool, Routes{"t": {"a": func(context.Context, Event) error { return nil }, "b": func(context.Context, Event) error { return nil }}}, time.Now)
			var originalID int64
			if err := pool.QueryRow(ctx, `SELECT id FROM outbox_events WHERE target=''`).Scan(&originalID); err != nil {
				t.Fatal(err)
			}
			if failure == "second target insert" {
				// A fresh queue has original id 1. The first target consumes id 2 and the next
				// nextval fails. Sequence increments survive rollback, proving partial work ran.
				if _, err := pool.Exec(ctx, `ALTER SEQUENCE outbox_events_id_seq MAXVALUE 2`); err != nil {
					t.Fatal(err)
				}
			} else {
				// Only a test fixture references the original event. Target INSERTs succeed,
				// then DELETE violates this FK, exercising rollback after all inserts.
				if _, err := pool.Exec(ctx, `CREATE TABLE fanout_delete_blocker(event_id bigint REFERENCES outbox_events(id)); INSERT INTO fanout_delete_blocker(event_id) SELECT id FROM outbox_events WHERE target=''`); err != nil {
					t.Fatal(err)
				}
			}
			if found, err := d.one(ctx); !found || err == nil {
				t.Fatalf("forced failure did not occur: found=%v err=%v", found, err)
			}
			var total, originals, targets int
			if err := pool.QueryRow(ctx, `SELECT count(*),count(*) FILTER(WHERE id=$1 AND target=''),count(*) FILTER(WHERE target<>'') FROM outbox_events`, originalID).Scan(&total, &originals, &targets); err != nil {
				t.Fatal(err)
			}
			if total != 1 || originals != 1 || targets != 0 {
				t.Fatalf("partial fanout survived rollback: total=%d originals=%d targets=%d", total, originals, targets)
			}
			var lastID int64
			if err := pool.QueryRow(ctx, `SELECT last_value FROM outbox_events_id_seq`).Scan(&lastID); err != nil {
				t.Fatal(err)
			}
			minimum := int64(2)
			if failure == "original delete" {
				minimum = 3
			}
			if lastID < minimum {
				t.Fatalf("failure happened before expected inserts: sequence=%d", lastID)
			}
			if failure == "second target insert" {
				if _, err := pool.Exec(ctx, `ALTER SEQUENCE outbox_events_id_seq NO MAXVALUE`); err != nil {
					t.Fatal(err)
				}
			} else {
				if _, err := pool.Exec(ctx, `DROP TABLE fanout_delete_blocker`); err != nil {
					t.Fatal(err)
				}
			}
			if found, err := d.one(ctx); err != nil || !found {
				t.Fatalf("retry fanout: %v %v", found, err)
			}
			if err := pool.QueryRow(ctx, `SELECT count(*),count(*) FILTER(WHERE target=''),count(DISTINCT target) FROM outbox_events`).Scan(&total, &originals, &targets); err != nil {
				t.Fatal(err)
			}
			if total != 2 || originals != 0 || targets != 2 {
				t.Fatalf("retry did not create full recipient set: %d %d %d", total, originals, targets)
			}
		})
	}
}

func configurationPool(t *testing.T) *pgxpool.Pool {
	t.Helper()
	p, err := pgxpool.New(t.Context(), "postgres://localhost/unused?pool_max_conns=66")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(p.Close)
	return p
}

func assertNoSessionLocks(t *testing.T, pool *pgxpool.Pool) {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), 3*time.Second)
	defer cancel()
	for {
		var n int
		if err := pool.QueryRow(ctx, `SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND database=(SELECT oid FROM pg_database WHERE datname=current_database())`).Scan(&n); err != nil {
			t.Fatal(err)
		}
		if n == 0 {
			return
		}
	}
}
func TestIntegrationSessionLockCleanupForEveryOutcome(t *testing.T) {
	for _, outcome := range []string{"success", "delivery failure", "cancel", "fanout", "unknown topic"} {
		t.Run(outcome, func(t *testing.T) {
			pool := database(t)
			entered := make(chan struct{})
			send := func(ctx context.Context, _ Event) error {
				if outcome == "delivery failure" {
					return errors.New("unavailable")
				}
				if outcome == "cancel" {
					close(entered)
					<-ctx.Done()
					return ctx.Err()
				}
				return nil
			}
			d := dispatcher(t, pool, Routes{"t": {"x": send}}, time.Now)
			topic := "t"
			if outcome == "unknown topic" {
				topic = "unknown"
			}
			publish(t, pool, topic, true)
			if _, err := d.one(t.Context()); err != nil {
				t.Fatal(err)
			}
			assertNoSessionLocks(t, pool)
			if outcome == "fanout" || outcome == "unknown topic" {
				return
			}
			if outcome == "cancel" {
				call, cancel := context.WithCancel(t.Context())
				done := make(chan error, 1)
				go func() { _, err := d.one(call); done <- err }()
				deadline, stop := context.WithTimeout(t.Context(), 3*time.Second)
				defer stop()
				select {
				case <-entered:
				case <-deadline.Done():
					cancel()
					t.Fatal("RPC not entered before cancellation")
				}
				cancel()
				if err := <-done; !errors.Is(err, context.Canceled) {
					t.Fatalf("actual worker cancellation not observed: %v", err)
				}
				if pending(t, pool) != 1 {
					t.Fatal("cancel lost durable row")
				}
			} else {
				if _, err := d.one(t.Context()); err != nil {
					t.Fatal(err)
				}
			}
			assertNoSessionLocks(t, pool)
		})
	}
}
func TestIntegrationSlowTargetDoesNotBlockHealthyTarget(t *testing.T) {
	pool := database(t)
	ctx, cancel := context.WithTimeout(t.Context(), 3*time.Second)
	defer cancel()
	slowEntered := make(chan struct{})
	healthy := make(chan struct{})
	d := dispatcher(t, pool, Routes{"t": {"a": func(ctx context.Context, _ Event) error { close(slowEntered); <-ctx.Done(); return ctx.Err() }, "b": func(context.Context, Event) error { close(healthy); return nil }}}, time.Now)
	publish(t, pool, "t", true)
	done := make(chan error, 1)
	go func() { done <- d.drainOnce(ctx) }()
	select {
	case <-slowEntered:
	case <-ctx.Done():
		t.Fatal("slow target not entered")
	}
	select {
	case <-healthy:
	case <-ctx.Done():
		t.Fatal("healthy target blocked by slow recipient")
	}
	cancel()
	if err := <-done; err != nil && !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	assertNoSessionLocks(t, pool)
}
func metricValue(t *testing.T, registry *prometheus.Registry, name, target string) float64 {
	t.Helper()
	families, err := registry.Gather()
	if err != nil {
		t.Fatal(err)
	}
	for _, family := range families {
		if family.GetName() != name {
			continue
		}
		for _, metric := range family.Metric {
			for _, label := range metric.Label {
				if label.GetName() == "target" && label.GetValue() == target {
					if metric.Gauge != nil {
						return metric.Gauge.GetValue()
					}
					return metric.Counter.GetValue()
				}
			}
		}
	}
	return 0
}
func TestIntegrationMetricValuesAndUnknownTopicScheduling(t *testing.T) {
	pool := database(t)
	now := time.Now().Add(time.Hour).Truncate(time.Microsecond)
	clock := func() time.Time { return now }
	registry := prometheus.NewRegistry()
	d, err := NewDispatcher(Options{Pool: pool, Routes: Routes{"t": {"b": func(context.Context, Event) error { return errors.New("secret peer") }}}, Now: clock, Logger: slog.New(slog.NewTextHandler(io.Discard, nil)), Registerer: registry})
	if err != nil {
		t.Fatal(err)
	}
	publish(t, pool, "unknown", true)
	publish(t, pool, "t", true)
	if _, err = pool.Exec(t.Context(), `INSERT INTO outbox_events(topic,event_key,payload,target,next_attempt_at,created_at) VALUES('t','key',''::bytea,'removed-secret-target',$1,$2)`, now, now.Add(-2*time.Hour)); err != nil {
		t.Fatal(err)
	}
	if err = d.drainOnce(t.Context()); err != nil {
		t.Fatal(err)
	}
	for target, want := range map[string]float64{"": 1, "b": 1, "unconfigured": 1} {
		if got := metricValue(t, registry, "finassist_outbox_rows", target); got != want {
			t.Fatalf("rows target %q=%v want%v", target, got, want)
		}
	}
	if got := metricValue(t, registry, "finassist_outbox_delivery_errors_total", "b"); got != 1 {
		t.Fatalf("delivery errors=%v", got)
	}
	age := metricValue(t, registry, "finassist_outbox_oldest_age_seconds", "")
	if age < 3590 {
		t.Fatalf("age uses retry date instead of created_at: %v", age)
	}
	var attempts int
	var last string
	var due time.Time
	if err = pool.QueryRow(t.Context(), `SELECT attempts,last_error,next_attempt_at FROM outbox_events WHERE topic='unknown'`).Scan(&attempts, &last, &due); err != nil {
		t.Fatal(err)
	}
	if attempts != 0 || last != "route_unconfigured" || !due.Equal(now.Add(5*time.Minute)) {
		t.Fatalf("unknown schedule %d %s %v", attempts, last, due)
	}
	now = now.Add(10 * time.Minute)
	if err = d.observe(t.Context()); err != nil {
		t.Fatal(err)
	}
	if got := metricValue(t, registry, "finassist_outbox_oldest_age_seconds", ""); got-age < 599 {
		t.Fatal("oldest age failed to advance")
	}
	families, err := registry.Gather()
	if err != nil {
		t.Fatal(err)
	}
	for _, family := range families {
		for _, metric := range family.Metric {
			for _, label := range metric.Label {
				if label.GetValue() == "removed-secret-target" {
					t.Fatal("unconfigured route leaked as label")
				}
			}
		}
	}
	if _, err = pool.Exec(t.Context(), `DELETE FROM outbox_events`); err != nil {
		t.Fatal(err)
	}
	if err = d.observe(t.Context()); err != nil {
		t.Fatal(err)
	}
	if got := metricValue(t, registry, "finassist_outbox_rows", ""); got != 0 {
		t.Fatal("stale queue gauge")
	}
}

func TestIntegrationMetricsObserveWhileRecipientBlocked(t *testing.T) {
	pool := database(t)
	entered := make(chan struct{})
	release := make(chan struct{})
	registry := prometheus.NewRegistry()
	d, err := NewDispatcher(Options{Pool: pool, Routes: Routes{"t": {"x": func(ctx context.Context, _ Event) error {
		close(entered)
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-release:
			return nil
		}
	}}}, Now: time.Now, Logger: slog.New(slog.NewTextHandler(io.Discard, nil)), Registerer: registry, PollInterval: time.Millisecond})
	if err != nil {
		t.Fatal(err)
	}
	publish(t, pool, "t", true)
	ctx, cancel := context.WithTimeout(t.Context(), 3*time.Second)
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- d.Run(ctx) }()
	select {
	case <-entered:
	case <-ctx.Done():
		t.Fatal("recipient not entered")
	}
	// Change created_at after delivery began. Only the independent observer can see
	// the new age while cycle is still waiting for this live RPC.
	if _, err = pool.Exec(ctx, `UPDATE outbox_events SET created_at=$1`, time.Now().Add(-2*time.Hour)); err != nil {
		t.Fatal(err)
	}
	for metricValue(t, registry, "finassist_outbox_oldest_age_seconds", "x") < 7190 {
		if ctx.Err() != nil {
			t.Fatal("metrics frozen behind blocked recipient")
		}
	}
	close(release)
	cancel()
	if err = <-done; !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
}

func TestIntegrationDispatcherPoolCapacityAndRouteLimits(t *testing.T) {
	pool, err := pgxpool.New(t.Context(), "postgres://localhost/unused?pool_max_conns=3")
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	o := Options{Pool: pool, Now: time.Now, Logger: slog.New(slog.NewTextHandler(io.Discard, nil)), Registerer: prometheus.NewRegistry(), Routes: Routes{"t": {"x": func(context.Context, Event) error { return nil }}}}
	if _, err = NewDispatcher(o); err == nil {
		t.Fatal("insufficient pool capacity accepted")
	}
	o.Pool = configurationPool(t)
	o.Routes = Routes{}
	for i := 0; i < 65; i++ {
		o.Routes[fmt.Sprintf("topic-%d", i)] = map[string]Delivery{"x": func(context.Context, Event) error { return nil }}
	}
	if _, err = NewDispatcher(o); err == nil {
		t.Fatal("unbounded topics accepted")
	}
	o.Routes = Routes{"t": {}}
	for i := 0; i < 65; i++ {
		o.Routes["t"][fmt.Sprintf("target-%d", i)] = func(context.Context, Event) error { return nil }
	}
	if _, err = NewDispatcher(o); err == nil {
		t.Fatal("unbounded targets accepted")
	}
}

func TestIntegrationEmptyPollReusesSafeConnection(t *testing.T) {
	pool := database(t)
	d := dispatcher(t, pool, Routes{"t": {"x": func(context.Context, Event) error { return nil }}}, time.Now)
	var before, after uint32
	if err := pool.QueryRow(t.Context(), `SELECT pg_backend_pid()`).Scan(&before); err != nil {
		t.Fatal(err)
	}
	if found, err := d.one(t.Context()); err != nil || found {
		t.Fatalf("empty poll %v %v", found, err)
	}
	if err := pool.QueryRow(t.Context(), `SELECT pg_backend_pid()`).Scan(&after); err != nil {
		t.Fatal(err)
	}
	if before != after {
		t.Fatal("empty poll destroyed a session without advisory locks")
	}
	assertNoSessionLocks(t, pool)
}

// drainOnce exercises the same production batch primitive synchronously for storage
// assertions. Lifecycle, notifications and cross-lane progress are tested through Run.
func (d *Dispatcher) drainOnce(ctx context.Context) error {
	if _, err := d.batch(ctx, "", nil); err != nil {
		return err
	}
	if _, err := d.drain(ctx, d.removedRoutes(), nil); err != nil {
		return err
	}
	var wg sync.WaitGroup
	errs := make(chan error, len(d.targetNames()))
	for _, target := range d.targetNames() {
		wg.Go(func() {
			_, err := d.batch(ctx, target, nil)
			if err != nil {
				errs <- err
			}
		})
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		return err
	}
	return d.observe(ctx)
}
func (d *Dispatcher) one(ctx context.Context) (bool, error) {
	for _, target := range append([]string{""}, d.targetNames()...) {
		found, err := d.oneFor(ctx, target)
		if errors.Is(err, errRetryScheduled) {
			err = nil
		}
		if found || err != nil {
			return found, err
		}
	}
	return false, nil
}

func TestIntegrationRunDeliversNewEventsWhileSlowSuccessfulTargetBusy(t *testing.T) {
	// Exactly targets+3 slots: two RPC lanes, fan-out, LISTEN and observer.
	pool := database(t, 5)
	ctx, cancel := context.WithTimeout(t.Context(), 4*time.Second)
	defer cancel()
	entered := make(chan struct{}, 2)
	release := make(chan struct{})
	healthy := make(chan struct{}, 2)
	var successful atomic.Int32
	slow := func(ctx context.Context, _ Event) error {
		entered <- struct{}{}
		select {
		case <-release:
			successful.Add(1)
			return nil
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	d := dispatcher(t, pool, Routes{"slow": {"a": slow}, "healthy": {"b": func(context.Context, Event) error { healthy <- struct{}{}; return nil }}}, time.Now)
	publish(t, pool, "slow", true)
	publish(t, pool, "slow", true)
	done := make(chan error, 1)
	go func() { done <- d.Run(ctx) }()
	wait := func(ch <-chan struct{}, what string) {
		t.Helper()
		select {
		case <-ch:
		case <-ctx.Done():
			t.Fatal(what)
		}
	}
	wait(entered, "slow RPC never started")
	publish(t, pool, "healthy", true)
	wait(healthy, "new healthy event blocked behind slow RPC")
	// The first slow call succeeds, and the second blocks. A second NEW healthy event
	// must still get its own fan-out and delivery without a shared batch barrier.
	select {
	case release <- struct{}{}:
	case <-ctx.Done():
		t.Fatal("slow success not released")
	}
	wait(entered, "second slow RPC never started")
	if successful.Load() != 1 {
		t.Fatal("first slow RPC did not succeed")
	}
	publish(t, pool, "healthy", true)
	wait(healthy, "new healthy event blocked behind second slow RPC")
	select {
	case release <- struct{}{}:
	case <-ctx.Done():
		t.Fatal("second slow success not released")
	}
	cancel()
	if err := <-done; !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	assertNoSessionLocks(t, pool)
}
func TestIntegrationRunRemovedRoutesDoNotStarveNewFanout(t *testing.T) {
	pool := database(t)
	// A hang guard, not a speed bound: each orphan row closes its locked session and opens a
	// new one, which takes tens of milliseconds under -race on a loaded runner.
	ctx, cancel := context.WithTimeout(t.Context(), 20*time.Second)
	defer cancel()
	healthy := make(chan struct{}, 1)
	// More than one batch of old orphan deliveries must not stop new fan-out.
	if _, err := pool.Exec(ctx, `INSERT INTO outbox_events(topic,event_key,payload,target,next_attempt_at,created_at) SELECT 'old','key',''::bytea,'removed-target',$1,$1 FROM generate_series(1,80)`, time.Now().Add(-time.Hour)); err != nil {
		t.Fatal(err)
	}
	publish(t, pool, "new", true)
	d := dispatcher(t, pool, Routes{"new": {"b": func(context.Context, Event) error { healthy <- struct{}{}; return nil }}}, time.Now)
	d.options.PollInterval = time.Hour // Progress must come from full-batch draining and wake signals.
	done := make(chan error, 1)
	go func() { done <- d.Run(ctx) }()
	select {
	case <-healthy:
	case <-ctx.Done():
		t.Fatal("old orphan deliveries starved new event")
	}
	// The removed-routes pass interleaves full batches with fan-out until all due orphans are classified.
	for classified := 0; classified != 80; {
		if err := pool.QueryRow(ctx, `SELECT count(*) FROM outbox_events WHERE target='removed-target' AND last_error='route_unconfigured'`).Scan(&classified); err != nil {
			t.Fatalf("only %d orphan routes classified: %v", classified, err)
		}
	}
	cancel()
	if err := <-done; !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	assertNoSessionLocks(t, pool)
}
func TestIntegrationPoolCapacityIncludesListenerFanoutAndObserver(t *testing.T) {
	pool := database(t, 4)
	options := Options{Pool: pool, Now: time.Now, Logger: slog.New(slog.NewTextHandler(io.Discard, nil)), Registerer: prometheus.NewRegistry(), Routes: Routes{"t": {"a": func(context.Context, Event) error { return nil }, "b": func(context.Context, Event) error { return nil }}}}
	if _, err := NewDispatcher(options); err == nil {
		t.Fatal("accepted targets+2 instead of targets+3 capacity")
	}
}
