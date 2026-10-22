package outbox

import (
	"context"
	"errors"
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

func database(t *testing.T) *pgxpool.Pool {
	t.Helper()
	dsn := pgtest.DSN(t)
	pgtest.Apply(t, dsn, MigrationsTable, Migrations)
	pool, err := pgxpool.New(t.Context(), dsn)
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
func TestTransactionalPersistenceAndFanout(t *testing.T) {
	pool := database(t)
	publish(t, pool, "settings", false)
	if pending(t, pool) != 0 {
		t.Fatal("rollback left event")
	}
	publish(t, pool, "settings", true)
	var a, b int
	d := dispatcher(t, pool, Routes{"settings": {"a": func(context.Context, Event) error { a++; return nil }, "b": func(context.Context, Event) error { b++; return nil }}}, time.Now)
	// A new dispatcher sees the committed event after the publisher/process is gone.
	if err := d.cycle(t.Context()); err != nil {
		t.Fatal(err)
	}
	if a != 1 || b != 1 || pending(t, pool) != 0 {
		t.Fatalf("fanout %d %d", a, b)
	}
}
func TestIndependentRetryAndUnknownRoutes(t *testing.T) {
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
	if err := d.cycle(t.Context()); err != nil {
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
	if err := d.cycle(t.Context()); err != nil {
		t.Fatal(err)
	}
	if b != 1 {
		t.Fatal("retried before due")
	}
	mu.Lock()
	now = now.Add(6 * time.Minute)
	mu.Unlock()
	if err := d.cycle(t.Context()); err != nil {
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
	if err = d.cycle(t.Context()); err != nil {
		t.Fatal(err)
	}
	if d.label("removed-secret-target") != "unconfigured" || pending(t, pool) != 1 {
		t.Fatal("removed route lost")
	}
}
func TestTwoPodsAndCancellationReleaseLock(t *testing.T) {
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
func TestRunLifecycle(t *testing.T) {
	pool := database(t)
	delivered := make(chan struct{}, 1)
	d := dispatcher(t, pool, Routes{"t": {"x": func(context.Context, Event) error { delivered <- struct{}{}; return nil }}}, time.Now)
	ctx, cancel := context.WithTimeout(t.Context(), 3*time.Second)
	defer cancel()
	done := make(chan error, 1)
	d.options.PollInterval = time.Hour // Delivery must wake from NOTIFY, not the poll timer.
	go func() { done <- d.Run(ctx) }()
	for {
		var listening bool
		if err := pool.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=current_database() AND query='LISTEN finassist_outbox' AND state='idle')`).Scan(&listening); err != nil {
			t.Fatal(err)
		}
		if listening {
			break
		}
	}
	publish(t, pool, "t", true)
	select {
	case <-delivered:
	case <-ctx.Done():
		t.Fatal("Run failed to deliver")
	}
	if err := d.Run(ctx); err == nil {
		t.Fatal("concurrent Run accepted")
	}
	cancel()
	if err := <-done; !errors.Is(err, context.Canceled) {
		t.Fatalf("shutdown %v", err)
	}
}
func TestConfiguration(t *testing.T) {
	pool := &pgxpool.Pool{}
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

func TestReadyRowBeyondLockedPage(t *testing.T) {
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
func TestDatabaseErrorsAndLostConnection(t *testing.T) {
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
		d.wait(ctx)
	})
}

func TestCommitNotification(t *testing.T) {
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

func TestMetricRegistrationRollback(t *testing.T) {
	registry := prometheus.NewRegistry()
	conflict := prometheus.NewGaugeVec(prometheus.GaugeOpts{Name: "finassist_outbox_oldest_age_seconds", Help: "Age of the oldest pending row by target."}, []string{"target"})
	if err := registry.Register(conflict); err != nil {
		t.Fatal(err)
	}
	_, err := NewDispatcher(Options{Pool: &pgxpool.Pool{}, Now: time.Now, Logger: slog.New(slog.NewTextHandler(io.Discard, nil)), Registerer: registry, Routes: Routes{"t": {"x": func(context.Context, Event) error { return nil }}}})
	if err == nil {
		t.Fatal("conflict ignored")
	}
	rows := prometheus.NewGaugeVec(prometheus.GaugeOpts{Name: "finassist_outbox_rows", Help: "Pending rows by configured target; unexpanded events use the empty target."}, []string{"target"})
	if err = registry.Register(rows); err != nil {
		t.Fatalf("partial constructor leaked collector: %v", err)
	}
}
