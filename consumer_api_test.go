package outbox_test

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"net"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/prometheus/client_golang/prometheus"

	"github.com/fin-assist/go-outbox"
	"github.com/fin-assist/go-outbox/internal/pgtest"

	settings "github.com/fin-assist/contracts/gen/go/finassist/usersettings/v1"
	identity "github.com/fin-assist/go-identity"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"
	"google.golang.org/protobuf/proto"
)

type serviceChecker struct{}

func (serviceChecker) VerifyService(_ context.Context, token string) (string, error) {
	if token == "user-service" {
		return "user", nil
	}
	if token == "foreign-service" {
		return "assistant", nil
	}
	return "", errors.New("invalid test token")
}

type userChecker struct{}

func (userChecker) VerifyUser(context.Context, string) (string, error) {
	return "", errors.New("system calls require no user token")
}

type settingsServer struct {
	settings.UnimplementedUserSettingsSyncServiceServer
	mu        sync.Mutex
	version   int32
	applied   int
	tombstone bool
}

func (s *settingsServer) SetUserTimezone(ctx context.Context, r *settings.SetUserTimezoneRequest) (*settings.SetUserTimezoneResponse, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if caller, ok := identity.Caller(ctx); !ok || caller != "user" {
		return nil, errors.New("unexpected caller")
	}
	md, _ := metadata.FromIncomingContext(ctx)
	if len(md.Get("authorization")) > 0 || len(md.Get("x-client-version")) > 0 {
		return nil, errors.New("user metadata leaked")
	}
	if !s.tombstone && r.SettingsVersion > s.version {
		s.version = r.SettingsVersion
		s.applied++
	}
	return &settings.SetUserTimezoneResponse{}, nil
}
func TestAPIServiceRunAndCallerTransactions(t *testing.T) {
	pool := database(t)
	auth, err := identity.NewServer(identity.AccessTable{settings.UserSettingsSyncService_SetUserTimezone_FullMethodName: {"user": identity.System}}, serviceChecker{}, userChecker{}, nil)
	if err != nil {
		t.Fatal(err)
	}
	listener := bufconn.Listen(1024 * 1024)
	server := grpc.NewServer(grpc.UnaryInterceptor(auth.Unary()))
	handler := &settingsServer{}
	settings.RegisterUserSettingsSyncServiceServer(server, handler)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	conn, err := grpc.NewClient("passthrough:///usage", grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) { return listener.Dial() }))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	path := filepath.Join(t.TempDir(), "projected-token")
	if err = os.WriteFile(path, []byte("user-service"), 0600); err != nil {
		t.Fatal(err)
	}
	source, err := identity.NewProjectedSource(path)
	if err != nil {
		t.Fatal(err)
	}
	send, err := outbox.GRPCDelivery(conn, source, settings.UserSettingsSyncService_SetUserTimezone_FullMethodName, func() proto.Message { return &settings.SetUserTimezoneRequest{} }, func() proto.Message { return &settings.SetUserTimezoneResponse{} })
	if err != nil {
		t.Fatal(err)
	}
	d := dispatcher(t, pool, outbox.Routes{"user.timezone_changed": {"assistant": send}}, time.Now)
	tx, err := pool.Begin(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	rolledBack, _ := outbox.NewPublisher(provider{tx}, time.Now)
	if err = rolledBack.Publish(t.Context(), "user.timezone_changed", "rolled-back", nil); err != nil {
		t.Fatal(err)
	}
	if err = tx.Rollback(t.Context()); err != nil {
		t.Fatal(err)
	}
	if pending(t, pool) != 0 {
		t.Fatal("caller rollback retained event")
	}
	ctx, cancel := context.WithCancel(t.Context())
	done := make(chan error, 1)
	go func() { done <- d.Run(ctx) }()
	t.Cleanup(func() {
		cancel()
		select {
		case err := <-done:
			if !errors.Is(err, context.Canceled) {
				t.Error(err)
			}
		case <-time.After(5 * time.Second):
			t.Error("Run did not stop")
		}
	})
	publishVersion := func(version int32) {
		t.Helper()
		tx, e := pool.Begin(t.Context())
		if e != nil {
			t.Fatal(e)
		}
		p, _ := outbox.NewPublisher(provider{tx}, time.Now)
		payload, e := proto.Marshal(&settings.SetUserTimezoneRequest{UserId: "00000000-0000-4000-8000-000000000001", Timezone: "Europe/Moscow", SettingsVersion: version})
		if e != nil {
			t.Fatal(e)
		}
		if e = p.Publish(t.Context(), "user.timezone_changed", "00000000-0000-4000-8000-000000000001", payload); e != nil {
			t.Fatal(e)
		}
		if e = tx.Commit(t.Context()); e != nil {
			t.Fatal(e)
		}
		awaitEmpty(t, pool)
	}
	publishVersion(2)
	publishVersion(2)
	publishVersion(1)
	handler.mu.Lock()
	if handler.applied != 1 || handler.version != 2 {
		t.Fatal("duplicate or old event applied")
	}
	handler.tombstone = true
	handler.mu.Unlock()
	publishVersion(3)
	handler.mu.Lock()
	if handler.applied != 1 {
		t.Fatal("tombstone mutated")
	}
	handler.mu.Unlock()
	if pending(t, pool) != 0 {
		t.Fatal("acknowledged deliveries retained")
	}
	// The actual generated client observes the contract's bare PERMISSION_DENIED for a foreign service.
	client := settings.NewUserSettingsSyncServiceClient(conn)
	ctx = metadata.NewOutgoingContext(t.Context(), metadata.Pairs("x-service-authorization", "Bearer foreign-service"))
	_, err = client.SetUserTimezone(ctx, &settings.SetUserTimezoneRequest{UserId: "00000000-0000-4000-8000-000000000001", SettingsVersion: 4})
	if status.Code(err) != codes.PermissionDenied || len(status.Convert(err).Details()) != 0 {
		t.Fatalf("foreign caller status %v", err)
	}
}

type provider struct{ tx pgx.Tx }

func (p provider) Tx(context.Context) (pgx.Tx, bool) { return p.tx, p.tx != nil }
func database(t *testing.T) *pgxpool.Pool {
	t.Helper()
	dsn := pgtest.DSN(t)
	pgtest.Apply(t, dsn, outbox.MigrationsTable, outbox.Migrations)
	config, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		t.Fatal(err)
	}
	config.MaxConns = 8
	pool, err := pgxpool.NewWithConfig(t.Context(), config)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(pool.Close)
	return pool
}
func dispatcher(t *testing.T, pool *pgxpool.Pool, routes outbox.Routes, clock func() time.Time) *outbox.Dispatcher {
	t.Helper()
	d, err := outbox.NewDispatcher(outbox.Options{Pool: pool, Routes: routes, Now: clock, Logger: slog.New(slog.NewTextHandler(io.Discard, nil)), Registerer: prometheus.NewRegistry(), PollInterval: 10 * time.Millisecond})
	if err != nil {
		t.Fatal(err)
	}
	return d
}
func pending(t *testing.T, pool *pgxpool.Pool) int {
	t.Helper()
	var n int
	if err := pool.QueryRow(t.Context(), "SELECT count(*) FROM outbox_events").Scan(&n); err != nil {
		t.Fatal(err)
	}
	return n
}
func awaitEmpty(t *testing.T, pool *pgxpool.Pool) {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()
	ticker := time.NewTicker(5 * time.Millisecond)
	defer ticker.Stop()
	for {
		if pending(t, pool) == 0 {
			return
		}
		select {
		case <-ctx.Done():
			t.Fatal("durable events were not acknowledged")
		case <-ticker.C:
		}
	}
}

func TestAPIDurableFailureRecoveryAndUnknownRoute(t *testing.T) {
	pool := database(t)
	var clock atomic.Int64
	clock.Store(time.Now().Unix())
	now := func() time.Time { return time.Unix(clock.Load(), 0) }
	var healthy atomic.Bool
	failed := make(chan struct{}, 1)
	delivered := make(chan outbox.Event, 1)
	routes := outbox.Routes{"settings": {"assistant": func(_ context.Context, e outbox.Event) error {
		if !healthy.Load() {
			select {
			case failed <- struct{}{}:
			default:
			}
			return errors.New("secret payload")
		}
		select {
		case delivered <- e:
		default:
		}
		return nil
	}}}
	d := dispatcher(t, pool, routes, now)
	publish := func(topic string) {
		t.Helper()
		tx, err := pool.Begin(t.Context())
		if err != nil {
			t.Fatal(err)
		}
		p, err := outbox.NewPublisher(provider{tx}, now)
		if err != nil {
			t.Fatal(err)
		}
		if err = p.Publish(t.Context(), topic, "user", []byte("durable")); err != nil {
			t.Fatal(err)
		}
		if err = tx.Commit(t.Context()); err != nil {
			t.Fatal(err)
		}
	}
	publish("settings")
	publish("future-topic")
	ctx, cancel := context.WithCancel(t.Context())
	done := make(chan error, 1)
	go func() { done <- d.Run(ctx) }()
	t.Cleanup(func() {
		cancel()
		select {
		case err := <-done:
			if !errors.Is(err, context.Canceled) {
				t.Error(err)
			}
		case <-time.After(5 * time.Second):
			t.Error("Run did not stop")
		}
	})
	select {
	case <-failed:
	case <-time.After(5 * time.Second):
		t.Fatal("failure not delivered")
	}
	deadline, cancelWait := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancelWait()
	ticker := time.NewTicker(5 * time.Millisecond)
	defer ticker.Stop()
	for {
		var retries, unknown int
		err := pool.QueryRow(t.Context(), `SELECT count(*) FILTER(WHERE last_error='delivery_failed' AND attempts=1),count(*) FILTER(WHERE last_error='route_unconfigured') FROM outbox_events`).Scan(&retries, &unknown)
		if err != nil {
			t.Fatal(err)
		}
		if retries == 1 && unknown == 1 {
			break
		}
		select {
		case <-deadline.Done():
			t.Fatal("failure classifications not persisted")
		case <-ticker.C:
		}
	}
	var leaked int
	if err := pool.QueryRow(t.Context(), `SELECT count(*) FROM outbox_events WHERE last_error LIKE '%secret%'`).Scan(&leaked); err != nil || leaked != 0 {
		t.Fatal("peer error leaked", err)
	}
	if err := d.Run(t.Context()); err == nil {
		t.Fatal("concurrent Run admitted")
	}
	healthy.Store(true)
	clock.Add(3600)
	select {
	case e := <-delivered:
		if e.Key != "user" || string(e.Payload) != "durable" {
			t.Fatal("durable event changed")
		}
	case <-time.After(5 * time.Second):
		t.Fatal("retry was not recovered")
	}
	for {
		var n int
		if err := pool.QueryRow(t.Context(), `SELECT count(*) FROM outbox_events WHERE topic='settings'`).Scan(&n); err != nil {
			t.Fatal(err)
		}
		if n == 0 {
			break
		}
		select {
		case <-deadline.Done():
			t.Fatal("successful retry not acknowledged")
		case <-ticker.C:
		}
	}
}

func TestAPIConfigurationAndPublisherContracts(t *testing.T) {
	pool := database(t)
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	send := func(context.Context, outbox.Event) error { return nil }
	for _, change := range []func(*outbox.Options){func(o *outbox.Options) { o.Pool = nil }, func(o *outbox.Options) { o.Now = nil }, func(o *outbox.Options) { o.Logger = nil }, func(o *outbox.Options) { o.Registerer = nil }, func(o *outbox.Options) { o.Routes = nil }, func(o *outbox.Options) { o.PollInterval = -time.Second }, func(o *outbox.Options) { o.BatchSize = 51 }, func(o *outbox.Options) { o.Routes = outbox.Routes{"bad\x00topic": {"assistant": send}} }, func(o *outbox.Options) { o.Routes = outbox.Routes{"settings": {"assistant": nil}} }, func(o *outbox.Options) { o.Routes = outbox.Routes{"settings": {}} }} {
		o := outbox.Options{Pool: pool, Now: time.Now, Logger: logger, Registerer: prometheus.NewRegistry(), Routes: outbox.Routes{"settings": {"assistant": send}}}
		change(&o)
		if _, err := outbox.NewDispatcher(o); err == nil {
			t.Fatal("invalid service configuration accepted")
		}
	}
	if _, err := outbox.NewPublisher(nil, time.Now); err == nil {
		t.Fatal("missing transaction provider accepted")
	}
	if _, err := outbox.NewPublisher(provider{}, nil); err == nil {
		t.Fatal("missing clock accepted")
	}
	p, err := outbox.NewPublisher(provider{}, time.Now)
	if err != nil {
		t.Fatal(err)
	}
	if err = p.Publish(t.Context(), "settings", "user", nil); !errors.Is(err, outbox.ErrNoTransaction) {
		t.Fatal(err)
	}
	for _, topic := range []string{"", strings.Repeat("x", 201), "bad\x00topic", string([]byte{0xff})} {
		if err := p.Publish(t.Context(), topic, "user", nil); !errors.Is(err, outbox.ErrInvalidEvent) {
			t.Fatal("bad topic accepted")
		}
	}
	tx, err := pool.Begin(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	publisher, err := outbox.NewPublisher(provider{tx}, time.Now)
	if err != nil {
		t.Fatal(err)
	}
	if err = tx.Rollback(t.Context()); err != nil {
		t.Fatal(err)
	}
	if err = publisher.Publish(t.Context(), "settings", "user", nil); err == nil || strings.Contains(err.Error(), "SQL") {
		t.Fatal("closed transaction accepted or diagnostic leaked", err)
	}
}

// FIN-281: a recipient panic is a failed delivery; the service process, Run and other
// recipients keep working and the poisoned event stays durable for retry.
func TestAPIRecipientPanicDoesNotStopService(t *testing.T) {
	pool := database(t)
	healthy := make(chan outbox.Event, 1)
	routes := outbox.Routes{"user.deleted": {
		"analytics": func(context.Context, outbox.Event) error { panic("PRIVATE payload") },
		"assistant": func(_ context.Context, e outbox.Event) error { healthy <- e; return nil },
	}}
	d := dispatcher(t, pool, routes, time.Now)
	tx, err := pool.Begin(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	p, err := outbox.NewPublisher(provider{tx}, time.Now)
	if err != nil {
		t.Fatal(err)
	}
	if err = p.Publish(t.Context(), "user.deleted", "user", []byte("purge")); err != nil {
		t.Fatal(err)
	}
	if err = tx.Commit(t.Context()); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(t.Context())
	done := make(chan error, 1)
	go func() { done <- d.Run(ctx) }()
	select {
	case e := <-healthy:
		if string(e.Payload) != "purge" {
			t.Fatal("healthy recipient got another event")
		}
	case <-time.After(5 * time.Second):
		t.Fatal("healthy recipient was not served next to a panicking one")
	}
	deadline := time.After(5 * time.Second)
	for {
		var target, lastError string
		var attempts int
		err := pool.QueryRow(t.Context(), `SELECT target,attempts,last_error FROM outbox_events`).Scan(&target, &attempts, &lastError)
		if err == nil && target == "analytics" && attempts >= 1 && lastError == "delivery_failed" {
			break
		}
		if err != nil && !errors.Is(err, pgx.ErrNoRows) {
			t.Fatal(err)
		}
		select {
		case <-deadline:
			t.Fatalf("panicking delivery not kept for retry: %q %d %q %v", target, attempts, lastError, err)
		case <-time.After(5 * time.Millisecond):
		}
	}
	cancel()
	select {
	case err := <-done:
		if !errors.Is(err, context.Canceled) {
			t.Fatal(err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("Run did not stop after a recipient panic")
	}
}
