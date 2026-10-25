package outbox

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"runtime"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// eventually checks cond every 20ms until it holds, failing at the context deadline. The
// pause keeps the check from loading the database the dispatcher under test uses.
func eventually(t *testing.T, ctx context.Context, what string, cond func(context.Context) (bool, error)) {
	t.Helper()
	ticker := time.NewTicker(20 * time.Millisecond)
	defer ticker.Stop()
	for {
		ok, err := cond(ctx)
		if ok {
			return
		}
		select {
		case <-ticker.C:
		case <-ctx.Done():
			t.Fatalf("%s: %v %v", what, err, ctx.Err())
		}
	}
}

// classifiedRemoved counts rows of the removed target "gone" marked by the removed-routes pass.
func classifiedRemoved(pool *pgxpool.Pool, target string) func(context.Context) (int, error) {
	return func(ctx context.Context) (int, error) {
		var n int
		err := pool.QueryRow(ctx, `SELECT count(*) FROM outbox_events WHERE target=$1 AND last_error='route_unconfigured'`, target).Scan(&n)
		return n, err
	}
}

// planNode is the part of EXPLAIN (FORMAT JSON) output the plan assertions read.
type planNode struct {
	NodeType  string     `json:"Node Type"`
	IndexName string     `json:"Index Name"`
	Plans     []planNode `json:"Plans"`
}

// scans lists the relation scans of a plan as "Node Type[:Index Name]".
func (n planNode) scans() []string {
	var result []string
	if strings.HasSuffix(n.NodeType, "Scan") {
		scan := n.NodeType
		if n.IndexName != "" {
			scan += ":" + n.IndexName
		}
		result = append(result, scan)
	}
	for _, child := range n.Plans {
		result = append(result, child.scans()...)
	}
	return result
}

// explain plans a candidate query exactly as the dispatcher sends it: an unnamed statement
// with bound parameters (pgx.QueryExecModeExec), so PostgreSQL plans it for the actual lane.
func explain(t *testing.T, pool *pgxpool.Pool, query string, args ...any) []string {
	t.Helper()
	var raw []byte
	if err := pool.QueryRow(t.Context(), `EXPLAIN (FORMAT JSON) `+query, append([]any{pgx.QueryExecModeExec}, args...)...).Scan(&raw); err != nil {
		t.Fatal(err)
	}
	var plans []struct {
		Plan planNode `json:"Plan"`
	}
	if err := json.Unmarshal(raw, &plans); err != nil || len(plans) != 1 {
		t.Fatalf("plan %s: %v", raw, err)
	}
	return plans[0].Plan.scans()
}

// A large ready backlog of one target must not make other lanes scan it: every lane query,
// including unexpanded events (target ”), is an index scan of the lane index.
func TestIntegrationLaneQueriesUseTargetIndexUnderForeignBacklog(t *testing.T) {
	pool := database(t)
	ctx := t.Context()
	now := time.Now()
	if _, err := pool.Exec(ctx, `INSERT INTO outbox_events(topic,event_key,payload,target,next_attempt_at,created_at) SELECT 't','key',''::bytea,'a',$1::timestamptz-g*interval '1 millisecond',$1 FROM generate_series(1,200000) g`, now); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(ctx, `INSERT INTO outbox_events(topic,event_key,payload,target,next_attempt_at,created_at) VALUES('t','key',''::bytea,'b',$1,$1),('t','key',''::bytea,'',$1,$1),('t','key',''::bytea,'gone',$1,$1)`, now); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(ctx, `ANALYZE outbox_events`); err != nil {
		t.Fatal(err)
	}
	var valid int
	if err := pool.QueryRow(ctx, `SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid WHERE c.relname IN ('outbox_events_lane','outbox_events_due') AND i.indisvalid`).Scan(&valid); err != nil || valid != 2 {
		t.Fatalf("lane and due indexes valid: %d %v", valid, err)
	}
	// Repeated planning never drifts to the foreign backlog: each execution is planned anew.
	for range 7 {
		for _, target := range []string{"b", ""} {
			if scans := explain(t, pool, laneCandidates, now, time.Time{}, int64(0), target); len(scans) != 1 || scans[0] != "Index Scan:outbox_events_lane" {
				t.Fatalf("lane %q plan %v", target, scans)
			}
		}
	}
	// The backlogged target itself may page through either index; it must not scan the table.
	if own := explain(t, pool, laneCandidates, now, time.Time{}, int64(0), "a"); len(own) != 1 || !strings.HasPrefix(own[0], "Index Scan:") {
		t.Fatalf("backlogged lane plan %v", own)
	} else {
		t.Logf("lane \"a\": %v", own)
	}
	// <>ALL cannot use the lane index; this is why the removed-routes pass is rare.
	removed := explain(t, pool, removedCandidates, now, time.Time{}, int64(0), []string{"a", "b"})
	if len(removed) != 1 || removed[0] == "Index Scan:outbox_events_lane" {
		t.Fatalf("removed-routes plan %v", removed)
	}
	t.Logf("removed routes: %v", removed)
	// The selections still find exactly their own rows under the backlog.
	d := dispatcher(t, pool, Routes{"t": {"a": func(context.Context, Event) error { return nil }, "b": func(context.Context, Event) error { return nil }}}, func() time.Time { return now })
	for _, c := range []struct {
		s    selection
		want string
	}{{d.lane("b"), "b"}, {d.lane(""), ""}, {d.removedRoutes(), "gone"}} {
		s, want := c.s, c.want
		var target string
		if err := pool.QueryRow(ctx, `SELECT o.target FROM outbox_events o JOIN (`+strings.TrimSuffix(s.query, " FOR UPDATE SKIP LOCKED")+`) c USING (id) LIMIT 1`, now, time.Time{}, int64(0), s.arg).Scan(&target); err != nil || target != want {
			t.Fatalf("selection found %q want %q: %v", target, want, err)
		}
	}
}

// The dispatcher's candidate query must never become a cached prepared statement on its
// session: after more executions than PostgreSQL's custom-plan threshold (five), no named
// statement with a reusable, possibly generic, plan exists.
func TestIntegrationCandidateQueryIsNotCachedOnSession(t *testing.T) {
	pool := database(t)
	ctx := t.Context()
	lease, err := pool.Acquire(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer lease.Release()
	d := dispatcher(t, pool, Routes{"t": {"b": func(context.Context, Event) error { return nil }}}, time.Now)
	for _, s := range []selection{d.lane("b"), d.lane(""), d.removedRoutes()} {
		for range 7 {
			lock := false
			if found, err := d.dispatch(ctx, lease, s, &lock); found || err != nil || lock {
				t.Fatalf("empty lane %v %v %v", found, err, lock)
			}
		}
	}
	var cached int
	if err := lease.QueryRow(ctx, `SELECT count(*) FROM pg_prepared_statements WHERE statement LIKE '%FROM outbox_events%' AND statement NOT LIKE '%pg_prepared_statements%'`).Scan(&cached); err != nil || cached != 0 {
		t.Fatalf("candidate statements cached on session: %d %v", cached, err)
	}
}

// Rows of a target removed from the configuration are only rescheduled by the removed-routes
// pass, which leaves configured lanes and unexpanded events alone; once the route is restored,
// the target's own lane delivers them.
func TestIntegrationRemovedRoutesPassReschedulesAndRestoredRouteDelivers(t *testing.T) {
	pool := database(t)
	ctx := t.Context()
	now := time.Now().Add(time.Hour).Truncate(time.Microsecond)
	clock := func() time.Time { return now }
	if _, err := pool.Exec(ctx, `INSERT INTO outbox_events(topic,event_key,payload,target,next_attempt_at,created_at) VALUES('t','key',''::bytea,'gone',$1,$1),('t','key',''::bytea,'gone',$1,$1),('t','key',''::bytea,'b',$1,$1),('t','key',''::bytea,'',$1,$1)`, now); err != nil {
		t.Fatal(err)
	}
	var mu sync.Mutex
	sent := map[string]int{}
	send := func(target string) Delivery {
		return func(context.Context, Event) error { mu.Lock(); defer mu.Unlock(); sent[target]++; return nil }
	}
	d := dispatcher(t, pool, Routes{"t": {"b": send("b")}}, clock)
	d.options.BatchSize = 1
	due, more := d.removedPass(ctx, time.Time{})
	if !more || !due.IsZero() {
		t.Fatalf("full removed batch must stay due: %v %v", due, more)
	}
	due, more = d.removedPass(ctx, due)
	if !more || !due.IsZero() {
		t.Fatalf("second full removed batch must stay due: %v %v", due, more)
	}
	due, more = d.removedPass(ctx, due)
	if more || !due.Equal(now.Add(removedRoutesInterval)) {
		t.Fatalf("drained pass must sleep for the interval: %v %v", due, more)
	}
	var gone int
	if err := pool.QueryRow(ctx, `SELECT count(*) FROM outbox_events WHERE target='gone' AND last_error='route_unconfigured' AND attempts=1 AND next_attempt_at>$1`, now).Scan(&gone); err != nil || gone != 2 {
		t.Fatalf("removed rows rescheduled: %d %v", gone, err)
	}
	var untouched int
	if err := pool.QueryRow(ctx, `SELECT count(*) FROM outbox_events WHERE target IN ('b','') AND attempts=0 AND last_error='' AND next_attempt_at=$1`, now).Scan(&untouched); err != nil || untouched != 2 || sent["b"] != 0 {
		t.Fatalf("removed pass touched configured lanes: %d %d %v", untouched, sent["b"], err)
	}
	// Not yet due: the pass issues no query and keeps its schedule.
	if again, more := d.removedPass(ctx, due); more || !again.Equal(due) {
		t.Fatal("pass ran before it was due")
	}
	now = now.Add(time.Hour)
	restored := dispatcher(t, pool, Routes{"t": {"b": send("b"), "gone": send("gone")}}, clock)
	if err := restored.drainOnce(ctx); err != nil {
		t.Fatal(err)
	}
	// The unexpanded event fans out to both targets; the two old rows go through the gone lane.
	if sent["gone"] != 3 || sent["b"] != 2 {
		t.Fatalf("restored route deliveries %v", sent)
	}
	if n := pending(t, pool); n != 0 {
		t.Fatalf("%d rows left", n)
	}
	assertNoSessionLocks(t, pool)
}

// The migration builds the lane index with CREATE INDEX CONCURRENTLY: the runner sends the
// file outside a transaction block, and the index is usable and valid; down removes it.
func TestIntegrationLaneIndexMigrationRunsConcurrently(t *testing.T) {
	pool := database(t)
	ctx := t.Context()
	var definition string
	if err := pool.QueryRow(ctx, `SELECT pg_get_indexdef(i.indexrelid) FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid WHERE c.relname='outbox_events_lane' AND i.indisvalid AND i.indisready`).Scan(&definition); err != nil {
		t.Fatal(err)
	}
	if !strings.HasSuffix(definition, "(target, next_attempt_at, id)") {
		t.Fatalf("lane index %s", definition)
	}
	// The same statement inside an explicit transaction is refused, so the migration's success
	// proves the runner did not wrap it.
	tx, err := pool.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = tx.Rollback(context.WithoutCancel(ctx)) }()
	_, err = tx.Exec(ctx, `CREATE INDEX CONCURRENTLY IF NOT EXISTS fin292_probe ON outbox_events (target)`, pgx.QueryExecModeSimpleProtocol)
	if err == nil || !strings.Contains(err.Error(), "transaction block") {
		t.Fatalf("CONCURRENTLY accepted inside a transaction: %v", err)
	}
}

// With PollInterval longer than the removed-routes cadence and no notifications, Run still
// wakes for the next pass: the first pass classifies a due removed-route row at start, and
// only a later pass, woken by the cadence rather than the hour-long poll, classifies it again
// once its retry is due.
func TestIntegrationRunWakesForRemovedRoutesPassDespiteLongPoll(t *testing.T) {
	pool := database(t)
	ctx, cancel := context.WithTimeout(t.Context(), 20*time.Second)
	defer cancel()
	// Due at the first pass; a plain INSERT sends no notification.
	if _, err := pool.Exec(ctx, `INSERT INTO outbox_events(topic,event_key,payload,target,next_attempt_at,created_at) VALUES('t','key',''::bytea,'gone',now(),now())`); err != nil {
		t.Fatal(err)
	}
	d := dispatcher(t, pool, Routes{"t": {"b": func(context.Context, Event) error { return nil }}}, time.Now)
	d.options.PollInterval = time.Hour
	d.removedInterval = 50 * time.Millisecond
	done := make(chan error, 1)
	go func() { done <- d.Run(ctx) }()
	eventually(t, ctx, "removed row not classified twice before the hour-long poll", func(ctx context.Context) (bool, error) {
		var attempts int
		err := pool.QueryRow(ctx, `SELECT attempts FROM outbox_events WHERE target='gone' AND last_error='route_unconfigured'`).Scan(&attempts)
		if errors.Is(err, pgx.ErrNoRows) {
			return false, nil
		}
		return attempts == 2, err
	})
	cancel()
	if err := <-done; !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	assertNoSessionLocks(t, pool)
}

// removedPassFailures counts warnings logged from removedPass, identified by the call site,
// so failures of the fan-out and target loops sharing the message are not counted.
type removedPassFailures struct{ n atomic.Int64 }

func (*removedPassFailures) Enabled(context.Context, slog.Level) bool { return true }
func (h *removedPassFailures) Handle(_ context.Context, r slog.Record) error {
	frame, _ := runtime.CallersFrames([]uintptr{r.PC}).Next()
	if strings.HasSuffix(frame.Function, ".removedPass") {
		h.n.Add(1)
	}
	return nil
}
func (h *removedPassFailures) WithAttrs([]slog.Attr) slog.Handler { return h }
func (h *removedPassFailures) WithGroup(string) slog.Handler      { return h }

// A PostgreSQL error during the removed-routes pass under Run is retried after the pass
// cadence, not after the hour-long PollInterval and not in a busy loop: while the table is
// unavailable each retry waits for the cadence, and once it is back the next retry classifies
// the removed-route row.
func TestIntegrationRunRetriesRemovedRoutesPassAfterDatabaseError(t *testing.T) {
	const interval = 100 * time.Millisecond
	pool := database(t)
	ctx, cancel := context.WithTimeout(t.Context(), 20*time.Second)
	defer cancel()
	if _, err := pool.Exec(ctx, `INSERT INTO outbox_events(topic,event_key,payload,target,next_attempt_at,created_at) VALUES('t','key',''::bytea,'gone',now(),now())`); err != nil {
		t.Fatal(err)
	}
	// Every dispatcher query fails with undefined_table until the table is renamed back.
	if _, err := pool.Exec(ctx, `ALTER TABLE outbox_events RENAME TO outbox_events_unavailable`); err != nil {
		t.Fatal(err)
	}
	restored := false
	defer func() {
		if !restored {
			_, _ = pool.Exec(context.WithoutCancel(ctx), `ALTER TABLE outbox_events_unavailable RENAME TO outbox_events`)
		}
	}()
	failures := &removedPassFailures{}
	d := dispatcher(t, pool, Routes{"t": {"b": func(context.Context, Event) error { return nil }}}, time.Now)
	d.options.Logger = slog.New(failures)
	d.options.PollInterval = time.Hour
	d.removedInterval = interval
	start := time.Now()
	done := make(chan error, 1)
	go func() { done <- d.Run(ctx) }()
	const retries = 4
	eventually(t, ctx, "removed pass not retried during the outage", func(context.Context) (bool, error) {
		return failures.n.Load() >= retries, nil
	})
	// Retry k follows k-1 cadence waits; a busy loop reaches it at once.
	if elapsed := time.Since(start); elapsed < (retries-1)*interval*9/10 {
		t.Fatalf("%d removed-pass failures in %v: retry does not wait", failures.n.Load(), elapsed)
	}
	if _, err := pool.Exec(ctx, `ALTER TABLE outbox_events_unavailable RENAME TO outbox_events`); err != nil {
		t.Fatal(err)
	}
	restored = true
	outage := time.Since(start)
	// Bounded retries: at most one per cadence wait during the outage, plus the one in flight.
	if n := failures.n.Load(); n > int64(outage/(interval*9/10))+2 {
		t.Fatalf("%d removed-pass failures in %v", n, outage)
	}
	classified := classifiedRemoved(pool, "gone")
	eventually(t, ctx, "removed row not classified after recovery", func(ctx context.Context) (bool, error) {
		n, err := classified(ctx)
		return n == 1, err
	})
	cancel()
	if err := <-done; !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	assertNoSessionLocks(t, pool)
}

// An event published while the removed-routes pass works through a backlog is fanned out
// and delivered after at most one more removed batch, long before the backlog is classified.
func TestIntegrationRunInterleavesNewFanoutWithRemovedRoutesBacklog(t *testing.T) {
	const backlog = 120
	pool := database(t)
	ctx, cancel := context.WithTimeout(t.Context(), 30*time.Second)
	defer cancel()
	if _, err := pool.Exec(ctx, `INSERT INTO outbox_events(topic,event_key,payload,target,next_attempt_at,created_at) SELECT 'old','key',''::bytea,'gone',now()-interval '1 hour',now() FROM generate_series(1,$1::int)`, backlog); err != nil {
		t.Fatal(err)
	}
	classified := classifiedRemoved(pool, "gone")
	delivered := make(chan int, 1)
	d := dispatcher(t, pool, Routes{"new": {"b": func(ctx context.Context, _ Event) error {
		n, err := classified(ctx)
		if err != nil {
			return err
		}
		delivered <- n
		return nil
	}}}, time.Now)
	d.options.PollInterval = time.Hour // Progress must come from batches and the publication's notification.
	d.options.BatchSize = 5
	done := make(chan error, 1)
	go func() { done <- d.Run(ctx) }()
	// Publish only once the pass is classifying the backlog.
	eventually(t, ctx, "removed pass never started", func(ctx context.Context) (bool, error) {
		n, err := classified(ctx)
		return n > 0, err
	})
	publish(t, pool, "new", true)
	select {
	case n := <-delivered:
		if n >= backlog {
			t.Fatalf("new event waited for the whole removed backlog (%d classified)", n)
		}
	case <-ctx.Done():
		t.Fatal("new event starved by the removed-routes pass")
	}
	eventually(t, ctx, "removed backlog not classified", func(ctx context.Context) (bool, error) {
		n, err := classified(ctx)
		return n == backlog, err
	})
	cancel()
	if err := <-done; !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	assertNoSessionLocks(t, pool)
}
