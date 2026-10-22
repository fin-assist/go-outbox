package outbox

import (
	"context"
	"errors"
	"net"
	"strings"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	health "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"
	"google.golang.org/protobuf/proto"
)

type tokenSource struct {
	value string
	err   error
}

func (s tokenSource) Token(context.Context) (string, error) { return s.value, s.err }

type healthServer struct {
	health.UnimplementedHealthServer
	check func(context.Context, *health.HealthCheckRequest) (*health.HealthCheckResponse, error)
}

func (s healthServer) Check(c context.Context, r *health.HealthCheckRequest) (*health.HealthCheckResponse, error) {
	return s.check(c, r)
}
func connection(t *testing.T, check func(context.Context, *health.HealthCheckRequest) (*health.HealthCheckResponse, error)) *grpc.ClientConn {
	t.Helper()
	listener := bufconn.Listen(1024 * 1024)
	server := grpc.NewServer()
	health.RegisterHealthServer(server, healthServer{check: check})
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	conn, err := grpc.NewClient("passthrough:///buf", grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) { return listener.Dial() }))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	return conn
}
func request() proto.Message  { return &health.HealthCheckRequest{} }
func response() proto.Message { return &health.HealthCheckResponse{} }
func TestGRPCDelivery(t *testing.T) {
	calls := 0
	conn := connection(t, func(ctx context.Context, r *health.HealthCheckRequest) (*health.HealthCheckResponse, error) {
		calls++
		md, _ := metadata.FromIncomingContext(ctx)
		if got := md.Get("x-service-authorization"); len(got) != 1 || got[0] != "Bearer service-token" {
			t.Errorf("auth %v", got)
		}
		if len(md.Get("authorization")) != 0 {
			t.Error("user token forwarded")
		}
		if r.Service != "user-key" {
			t.Error("payload lost")
		}
		return &health.HealthCheckResponse{}, nil
	})
	if _, err := GRPCDelivery(nil, tokenSource{}, "/x/y", request, response); err == nil {
		t.Fatal("nil conn")
	}
	if _, err := GRPCDelivery(conn, nil, "/x/y", request, response); err == nil {
		t.Fatal("nil source")
	}
	if _, err := GRPCDelivery(conn, tokenSource{}, "bad", request, response); err == nil {
		t.Fatal("bad method")
	}
	data, _ := proto.Marshal(&health.HealthCheckRequest{Service: "user-key"})
	event := Event{Payload: data}
	send, _ := GRPCDelivery(conn, tokenSource{value: "service-token"}, health.Health_Check_FullMethodName, request, response)
	ctx, cancel := context.WithTimeout(metadata.NewOutgoingContext(t.Context(), metadata.Pairs("authorization", "secret")), time.Second)
	defer cancel()
	if err := send(ctx, event); err != nil {
		t.Fatal(err)
	}
	if calls != 1 {
		t.Fatal("not delivered")
	}
	if err := send(ctx, Event{Payload: []byte{0xff}}); err == nil {
		t.Fatal("malformed protobuf")
	}
	for _, source := range []tokenSource{{}, {value: "bad\ntoken"}, {err: errors.New("secret")}} {
		bad, _ := GRPCDelivery(conn, source, health.Health_Check_FullMethodName, request, response)
		if err := bad(ctx, event); err == nil || strings.Contains(err.Error(), "secret") {
			t.Fatal("bad token or leak")
		}
	}
	nilMessage, _ := GRPCDelivery(conn, tokenSource{}, health.Health_Check_FullMethodName, func() proto.Message { return nil }, response)
	if nilMessage(ctx, event) == nil {
		t.Fatal("nil factory accepted")
	}
	failing := connection(t, func(context.Context, *health.HealthCheckRequest) (*health.HealthCheckResponse, error) {
		return nil, status.Error(codes.Internal, "secret dialogue")
	})
	send, _ = GRPCDelivery(failing, tokenSource{value: "token"}, health.Health_Check_FullMethodName, request, response)
	if err := send(ctx, event); err == nil || strings.Contains(err.Error(), "secret") {
		t.Fatal("peer error leaked")
	}
}
