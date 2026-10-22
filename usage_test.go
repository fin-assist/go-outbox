package outbox

import (
	"context"
	"errors"
	"net"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

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
func TestServiceUsage(t *testing.T) {
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
	send, err := GRPCDelivery(conn, source, settings.UserSettingsSyncService_SetUserTimezone_FullMethodName, func() proto.Message { return &settings.SetUserTimezoneRequest{} }, func() proto.Message { return &settings.SetUserTimezoneResponse{} })
	if err != nil {
		t.Fatal(err)
	}
	d := dispatcher(t, pool, Routes{"user.timezone_changed": {"assistant": send}}, time.Now)
	publishVersion := func(version int32) {
		t.Helper()
		tx, e := pool.Begin(t.Context())
		if e != nil {
			t.Fatal(e)
		}
		p, _ := NewPublisher(provider{tx}, time.Now)
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
		if e = d.drainOnce(t.Context()); e != nil {
			t.Fatal(e)
		}
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
	ctx := metadata.NewOutgoingContext(t.Context(), metadata.Pairs("x-service-authorization", "Bearer foreign-service"))
	_, err = client.SetUserTimezone(ctx, &settings.SetUserTimezoneRequest{UserId: "00000000-0000-4000-8000-000000000001", SettingsVersion: 4})
	if status.Code(err) != codes.PermissionDenied || len(status.Convert(err).Details()) != 0 {
		t.Fatalf("foreign caller status %v", err)
	}
}
