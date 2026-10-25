package outbox

import (
	"context"
	"errors"
	"log/slog"
	"math/rand/v2"
	"sync"
	"sync/atomic"
	"time"

	postgres "github.com/fin-assist/go-postgres"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/prometheus/client_golang/prometheus"
)

// Event is a durable protobuf event. Payload and key must never be logged or used as labels.
type Event struct {
	Topic, Key string
	Payload    []byte
}

// Delivery sends a system RPC. It must honour cancellation and return only after the call ends.
// It may run concurrently for distinct configured targets. A panic is recovered and handled as
// a failed delivery with the usual retry; runtime.Goexit is not. Recipients must handle
// duplicate and out-of-order versions, and acknowledge tombstoned users.
type Delivery func(context.Context, Event) error

// Routes maps topics to bounded, operator-configured target names and delivery methods.
// It is copied by NewDispatcher. Do not mutate a map while constructing a dispatcher.
type Routes map[string]map[string]Delivery

// Options configures primary-only dispatch. Dependencies are required; Now must be concurrency safe.
// Defaults: poll 3s, batch 50 per independent lane (maximum 50), RPC deadline 30s.
// The pool must reserve at least one connection per distinct target, plus three for
// fan-out, LISTEN and independent observation. Configuration is bounded to 64 topics/targets.
type Options struct {
	Pool         *pgxpool.Pool
	Routes       Routes
	Now          func() time.Time
	Logger       *slog.Logger
	Registerer   prometheus.Registerer
	PollInterval time.Duration
	BatchSize    int
}

// Dispatcher is safe for concurrent use except Run, which may run once at a time.
// It starts no goroutines until Run is called and uses only the supplied primary pool.
type Dispatcher struct {
	options Options
	pool    primaryPool
	// removedInterval is the removed-routes pass cadence: removedRoutesInterval in production.
	removedInterval time.Duration
	running         atomic.Bool
	count, age      *prometheus.GaugeVec
	failures        *prometheus.CounterVec
	observation     sync.Mutex
}

// NewDispatcher validates/copies configuration and registers bounded per-target metrics.
// Registration failures are returned; successfully registered metrics are rolled back on failure.
func NewDispatcher(o Options) (*Dispatcher, error) {
	if o.Pool == nil || o.Now == nil || o.Logger == nil || o.Registerer == nil || len(o.Routes) == 0 || len(o.Routes) > 64 {
		return nil, errors.New("outbox: dependencies and routes required")
	}
	if o.PollInterval == 0 {
		o.PollInterval = 3 * time.Second
	}
	if o.BatchSize == 0 {
		o.BatchSize = 50
	}
	if o.PollInterval < 0 || o.BatchSize < 1 || o.BatchSize > 50 {
		return nil, errors.New("outbox: invalid poll interval or batch size")
	}
	routes := Routes{}
	unique := map[string]bool{}
	for topic, targets := range o.Routes {
		if !validName(topic, 200) || len(targets) == 0 || len(targets) > 64 {
			return nil, errors.New("outbox: invalid route")
		}
		routes[topic] = map[string]Delivery{}
		for target, send := range targets {
			if !validName(target, 200) || send == nil {
				return nil, errors.New("outbox: invalid target")
			}
			routes[topic][target] = send
			unique[target] = true
		}
	}
	if len(unique) > 64 || o.Pool.Config().MaxConns < int32(len(unique)+3) {
		return nil, errors.New("outbox: too many targets or insufficient pool capacity")
	}
	o.Routes = routes
	d := &Dispatcher{options: o, pool: poolAdapter{o.Pool}, removedInterval: removedRoutesInterval, count: prometheus.NewGaugeVec(prometheus.GaugeOpts{Name: "finassist_outbox_rows", Help: "Pending rows by configured target; unexpanded events use the empty target."}, []string{"target"}), age: prometheus.NewGaugeVec(prometheus.GaugeOpts{Name: "finassist_outbox_oldest_age_seconds", Help: "Age of the oldest pending row by target."}, []string{"target"}), failures: prometheus.NewCounterVec(prometheus.CounterOpts{Name: "finassist_outbox_delivery_errors_total", Help: "Failed deliveries by configured target."}, []string{"target"})}
	collectors := []prometheus.Collector{d.count, d.age, d.failures}
	for i, c := range collectors {
		if err := o.Registerer.Register(c); err != nil {
			for _, prior := range collectors[:i] {
				o.Registerer.Unregister(prior)
			}
			return nil, err
		}
	}
	return d, nil
}

// Run dispatches until cancellation, reconnecting the listener/primary after database failures.
// No RPC runs inside a transaction. A short row-lock transaction obtains a session advisory lock;
// the dedicated connection holds it through RPC and acknowledgement. Process death frees the
// lock and leaves the row. An uncertain acknowledgement may lead to a duplicate delivery.
// ctx must be a service lifecycle context, without a go-postgres transaction. A transaction
// context returns postgres.ErrForeignTransaction before acquiring connections or starting workers.
// Cancellation is returned as ctx.Err(). A second concurrent Run returns an error.
func (d *Dispatcher) Run(ctx context.Context) error {
	if _, ok := postgres.Tx(ctx); ok {
		return postgres.ErrForeignTransaction
	}
	if !d.running.CompareAndSwap(false, true) {
		return errors.New("outbox: already running")
	}
	defer d.running.Store(false)
	runCtx, stop := context.WithCancel(ctx)
	var workers sync.WaitGroup
	defer func() { stop(); workers.Wait() }()
	workers.Go(func() { d.observeLoop(runCtx) })
	wakes := map[string]chan struct{}{}
	for _, target := range d.targetNames() {
		wake := make(chan struct{}, 1)
		wakes[target] = wake
		workers.Go(func() { d.targetLoop(runCtx, target, wake) })
	}
	wakeTargets := func() {
		for _, wake := range wakes {
			select {
			case wake <- struct{}{}:
			default:
			}
		}
	}
	// The zero time makes the first removed-routes pass run right after the first fan-out batch.
	var removedDue time.Time
	for ctx.Err() == nil {
		more, err := d.batch(runCtx, "", wakeTargets)
		if err != nil && ctx.Err() == nil {
			d.options.Logger.WarnContext(ctx, "outbox primary unavailable")
		}
		if ctx.Err() != nil {
			break
		}
		var removedMore bool
		removedDue, removedMore = d.removedPass(runCtx, removedDue)
		more = more || removedMore
		if ctx.Err() != nil {
			break
		}
		if more {
			continue
		}
		// Wake for the next removed-routes pass even when PollInterval is longer.
		d.wait(runCtx, wakeTargets, min(d.options.PollInterval, removedDue.Sub(d.options.Now())))
	}
	return ctx.Err()
}
func (d *Dispatcher) targetLoop(ctx context.Context, target string, wake <-chan struct{}) {
	for ctx.Err() == nil {
		more, err := d.batch(ctx, target, nil)
		if err != nil && ctx.Err() == nil {
			d.options.Logger.WarnContext(ctx, "outbox primary unavailable")
		}
		if more {
			continue
		}
		timer := time.NewTimer(d.options.PollInterval)
		select {
		case <-timer.C:
		case <-wake:
		case <-ctx.Done():
		}
		timer.Stop()
	}
}

// wait listens for a publication or sleeps for at most limit, then returns to the fan-out loop.
func (d *Dispatcher) wait(ctx context.Context, onWork func(), limit time.Duration) {
	c, err := d.pool.Acquire(ctx)
	if err != nil {
		d.pause(ctx, limit)
		return
	}
	defer func() {
		closeCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = c.closeDetached(closeCtx)
	}()
	if _, err = c.Exec(ctx, `LISTEN finassist_outbox`); err != nil {
		d.pause(ctx, limit)
		return
	}
	// Drain after LISTEN, so a publication between the first drain and LISTEN is visible.
	if more, _ := d.batch(ctx, "", onWork); more {
		return
	}
	wait, cancel := context.WithTimeout(ctx, limit)
	defer cancel()
	_ = c.waitForNotification(wait)
}

var errRetryScheduled = errors.New("outbox: delivery retry scheduled")

func (d *Dispatcher) targetNames() []string {
	names := map[string]bool{}
	for _, targets := range d.options.Routes {
		for name := range targets {
			names[name] = true
		}
	}
	result := make([]string, 0, len(names))
	for name := range names {
		result = append(result, name)
	}
	return result
}

// removedRoutesInterval is the cadence of the pass over rows whose target is no longer
// configured. It equals the maximum retry backoff, so every such row becomes due and is
// rescheduled at most one interval later. The pass handles one batch per fan-out batch
// while full, then sleeps for the interval.
const removedRoutesInterval = 5 * time.Minute

// Candidate queries of the lanes. Every lane pages by (next_attempt_at,id) with the lane
// argument as $4. laneCandidates serves one configured target or, with ”, unexpanded
// events through the (target,next_attempt_at,id) index. removedCandidates finds rows of
// targets absent from the current routes; <>ALL cannot use that index, so it runs rarely
// through the (next_attempt_at,id) index.
const (
	laneCandidates    = `SELECT id,next_attempt_at FROM outbox_events WHERE target=$4 AND next_attempt_at<=$1 AND (next_attempt_at,id)>($2,$3) ORDER BY next_attempt_at,id LIMIT 50 FOR UPDATE SKIP LOCKED`
	removedCandidates = `SELECT id,next_attempt_at FROM outbox_events WHERE target<>'' AND target<>ALL($4::text[]) AND next_attempt_at<=$1 AND (next_attempt_at,id)>($2,$3) ORDER BY next_attempt_at,id LIMIT 50 FOR UPDATE SKIP LOCKED`
)

// selection is a lane's candidate query and its $4 argument.
type selection struct {
	query string
	arg   any
}

// lane selects one configured target, or unexpanded events for the empty target.
func (d *Dispatcher) lane(target string) selection { return selection{laneCandidates, target} }

// removedRoutes selects deliveries whose target is not among the current routes.
func (d *Dispatcher) removedRoutes() selection {
	return selection{removedCandidates, d.targetNames()}
}

// removedPass handles one removed-routes batch once due is reached and returns the next due
// time and whether the batch was full. A full batch stays due, so it continues after the next
// fan-out batch; a database error retries after min(PollInterval, the interval); otherwise the
// pass sleeps for the interval. A returned time after Now is always in the future, so Run's
// bounded wait never spins.
func (d *Dispatcher) removedPass(ctx context.Context, due time.Time) (time.Time, bool) {
	if d.options.Now().Before(due) {
		return due, false
	}
	more, err := d.drain(ctx, d.removedRoutes(), nil)
	if err != nil {
		if ctx.Err() == nil {
			d.options.Logger.WarnContext(ctx, "outbox primary unavailable")
		}
		return d.options.Now().Add(min(d.options.PollInterval, d.removedInterval)), false
	}
	if more {
		return due, true
	}
	return d.options.Now().Add(d.removedInterval), false
}

// batch drains one lane. Empty target handles fan-out without RPC.
// A full batch immediately continues in that lane, independently of all other lanes.
func (d *Dispatcher) batch(ctx context.Context, target string, onWork func()) (bool, error) {
	return d.drain(ctx, d.lane(target), onWork)
}

// drain handles up to BatchSize rows of a selection and reports whether the batch was full.
func (d *Dispatcher) drain(ctx context.Context, s selection, onWork func()) (bool, error) {
	for range d.options.BatchSize {
		found, err := d.oneOf(ctx, s)
		if errors.Is(err, errRetryScheduled) {
			return false, nil
		}
		if err != nil {
			return false, err
		}
		if !found {
			return false, nil
		}
		if onWork != nil {
			onWork()
		}
	}
	return true, nil
}

type row struct {
	id       int64
	event    Event
	target   string
	attempts int
	created  time.Time
}

func (d *Dispatcher) oneFor(ctx context.Context, target string) (bool, error) {
	return d.oneOf(ctx, d.lane(target))
}

func (d *Dispatcher) oneOf(ctx context.Context, s selection) (bool, error) {
	c, err := d.pool.Acquire(ctx)
	if err != nil {
		return false, err
	}
	lockMayExist := false
	// A session lock must never escape to another pool borrower, even after cancellation.
	defer func() {
		if !lockMayExist {
			c.Release()
			return
		}
		closeCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = c.closeDetached(closeCtx)
	}()
	return d.dispatch(ctx, c, s, &lockMayExist)
}

// dispatchSession is one acquired connection lease, never the shared pool. Both
// transactions and their intervening delivery must retain the session advisory lock.
type dispatchSession interface {
	postgres.Beginner
	Exec(context.Context, string, ...any) (pgconn.CommandTag, error)
}

func (d *Dispatcher) dispatch(ctx context.Context, c dispatchSession, s selection, lockMayExist *bool) (bool, error) {
	transactions := postgres.New(c)
	var selected *row
	err := transactions.Run(ctx, func(ctx context.Context) error {
		tx, _ := transactions.Tx(ctx) // Run supplies this manager's transaction.
		var recovery bool
		if err := tx.QueryRow(ctx, `SELECT pg_is_in_recovery()`).Scan(&recovery); err != nil {
			return err
		}
		if recovery {
			return errors.New("outbox: primary required")
		}
		var afterTime time.Time
		var afterID int64
		for selected == nil {
			// An unnamed statement is planned for the actual lane each time. A cached prepared
			// statement switches to a generic plan after five executions, which ignores target
			// selectivity and pages a foreign backlog through outbox_events_due.
			rows, queryErr := tx.Query(ctx, s.query, pgx.QueryExecModeExec, d.options.Now(), afterTime, afterID, s.arg)
			if queryErr != nil {
				return queryErr
			}
			type candidate struct {
				id  int64
				due time.Time
			}
			var candidates []candidate
			for rows.Next() {
				var candidate candidate
				if err := rows.Scan(&candidate.id, &candidate.due); err != nil {
					rows.Close()
					return err
				}
				candidates = append(candidates, candidate)
			}
			rows.Close()
			if err := rows.Err(); err != nil {
				return err
			}
			if len(candidates) == 0 {
				break
			}
			for _, candidate := range candidates {
				afterTime, afterID = candidate.due, candidate.id
				var locked bool
				*lockMayExist = true
				if err := tx.QueryRow(ctx, `SELECT pg_try_advisory_lock(-$1::bigint)`, candidate.id).Scan(&locked); err != nil {
					return err
				}
				*lockMayExist = locked
				if locked {
					r := row{id: candidate.id}
					if err := tx.QueryRow(ctx, `SELECT topic,event_key,payload,target,attempts,created_at FROM outbox_events WHERE id=$1`, r.id).Scan(&r.event.Topic, &r.event.Key, &r.event.Payload, &r.target, &r.attempts, &r.created); err != nil {
						return err
					}
					selected = &r
					break
				}
			}
		}
		return nil
	})
	if err != nil {
		return false, err
	}
	if selected == nil {
		return false, nil
	}
	r := *selected
	if r.target == "" {
		return true, d.fanout(ctx, c, transactions, r)
	}
	send := d.options.Routes[r.event.Topic][r.target]
	var failure error
	if send == nil {
		failure = errors.New("route_unconfigured")
	} else {
		call, cancel := context.WithTimeout(ctx, 30*time.Second)
		failure = deliver(call, send, r.event)
		cancel()
		if errors.Is(failure, errDeliveryPanic) {
			// The panic value may carry payload data; only the configured target name is logged.
			d.options.Logger.WarnContext(ctx, "outbox delivery panicked", "target", r.target)
		}
	}
	if failure == nil {
		_, err = c.Exec(ctx, `DELETE FROM outbox_events WHERE id=$1`, r.id)
		return true, err
	}
	target := d.label(r.target)
	d.failures.WithLabelValues(target).Inc()
	// Persist a bounded classification only: peer errors can contain financial data/tokens.
	classification := "delivery_failed"
	if send == nil {
		classification = "route_unconfigured"
	}
	_, err = c.Exec(ctx, `UPDATE outbox_events SET attempts=LEAST(attempts::bigint+1,2147483647)::integer,next_attempt_at=$2,last_error=$3 WHERE id=$1`, r.id, d.options.Now().Add(backoff(r.attempts, retryJitter())), classification)
	if err != nil {
		return true, err
	}
	if send == nil {
		return true, nil
	}
	return true, errRetryScheduled
}

var errDeliveryPanic = errors.New("outbox: delivery panicked")

// deliver turns a recipient panic into a failed attempt, so one poisoned event retries with
// backoff instead of crashing every producer pod that claims it. runtime.Goexit cannot be
// intercepted and still unwinds through the caller's connection cleanup. The returned flag,
// not recover's value, detects the panic: with GODEBUG=panicnil=1 panic(nil) recovers as nil
// and would otherwise acknowledge (delete) an undelivered event.
func deliver(ctx context.Context, send Delivery, event Event) (err error) {
	returned := false
	defer func() {
		if !returned {
			_ = recover()
			err = errDeliveryPanic
		}
	}()
	err = send(ctx, event)
	returned = true
	return err
}
func (d *Dispatcher) fanout(ctx context.Context, c dispatchSession, transactions *postgres.Manager, r row) error {
	targets := d.options.Routes[r.event.Topic]
	if len(targets) == 0 {
		_, err := c.Exec(ctx, `UPDATE outbox_events SET next_attempt_at=$2,last_error='route_unconfigured' WHERE id=$1`, r.id, d.options.Now().Add(5*time.Minute))
		return err
	}
	return transactions.Run(ctx, func(ctx context.Context) error {
		tx, _ := transactions.Tx(ctx) // Run supplies this manager's transaction.
		for target := range targets {
			if _, err := tx.Exec(ctx, `INSERT INTO outbox_events(topic,event_key,payload,target,next_attempt_at,created_at) VALUES($1,$2,$3,$4,$5,$6)`, r.event.Topic, r.event.Key, r.event.Payload, target, d.options.Now(), r.created); err != nil {
				return err
			}
		}
		_, err := tx.Exec(ctx, `DELETE FROM outbox_events WHERE id=$1`, r.id)
		return err
	})
}
func backoff(attempt int, jitter float64) time.Duration {
	if attempt > 9 {
		attempt = 9
	}
	base := time.Second << attempt
	delay := time.Duration(float64(base) * (1 + jitter))
	if delay > 5*time.Minute {
		return 5 * time.Minute
	}
	return delay
}
func (d *Dispatcher) label(target string) string {
	if target == "" {
		return ""
	}
	for _, targets := range d.options.Routes {
		if _, ok := targets[target]; ok {
			return target
		}
	}
	return "unconfigured"
}
func (d *Dispatcher) observe(ctx context.Context) error {
	d.observation.Lock()
	defer d.observation.Unlock()
	rows, err := d.pool.Query(ctx, `SELECT target,count(*),min(created_at) FROM outbox_events GROUP BY target`)
	if err != nil {
		return err
	}
	defer rows.Close()
	counts := map[string]float64{}
	ages := map[string]float64{}
	for rows.Next() {
		var target string
		var count float64
		var oldest time.Time
		if err = rows.Scan(&target, &count, &oldest); err != nil {
			return err
		}
		label := d.label(target)
		counts[label] += count
		age := d.options.Now().Sub(oldest).Seconds()
		if age > ages[label] {
			ages[label] = age
		}
	}
	if err = rows.Err(); err != nil {
		return err
	}
	d.count.Reset()
	d.age.Reset()
	for target, count := range counts {
		d.count.WithLabelValues(target).Set(count)
		d.age.WithLabelValues(target).Set(ages[target])
	}
	return nil
}

func (d *Dispatcher) pause(ctx context.Context, limit time.Duration) {
	timer := time.NewTimer(limit)
	defer timer.Stop()
	select {
	case <-timer.C:
	case <-ctx.Done():
	}
}

// Retry jitter is scheduling noise, not a security boundary.
func retryJitter() float64 { return rand.Float64() } //nolint:gosec // No secrets or identifiers are generated.

func (d *Dispatcher) observeLoop(ctx context.Context) {
	interval := d.options.PollInterval
	if interval > 30*time.Second {
		interval = 30 * time.Second
	}
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for ctx.Err() == nil {
		if err := d.observe(ctx); err != nil && ctx.Err() == nil {
			d.options.Logger.WarnContext(ctx, "outbox observation unavailable")
		}
		select {
		case <-ticker.C:
		case <-ctx.Done():
			return
		}
	}
}
