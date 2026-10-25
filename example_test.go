package outbox_test

import (
	"context"
	"log/slog"
	"os"
	"time"

	postgres "github.com/fin-assist/go-postgres"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/prometheus/client_golang/prometheus"
	"google.golang.org/grpc"
	"google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/protobuf/proto"

	"github.com/fin-assist/go-outbox"
)

func ExamplePublisher_Publish() {
	ctx := context.Background()
	pool, _ := pgxpool.New(ctx, "postgres://localhost/service?target_session_attrs=read-write")
	defer pool.Close()
	transactions := postgres.New(pool) // One service-owned manager for this pool.
	publisher, _ := outbox.NewPublisher(transactions, time.Now)
	_ = transactions.Run(ctx, func(ctx context.Context) error {
		tx, _ := transactions.Tx(ctx)
		_ = tx // Perform the service's domain writes here before publishing.
		return publisher.Publish(ctx, "user.timezone_changed", "user-123", []byte{})
	})
}
func ExampleDispatcher_Run() {
	ctx := context.Background()
	pool, _ := pgxpool.New(ctx, "postgres://localhost/service?target_session_attrs=read-write")
	defer pool.Close()
	send := func(ctx context.Context, event outbox.Event) error { return nil } // Wire the generated RPC client.
	dispatcher, _ := outbox.NewDispatcher(outbox.Options{Pool: pool, Now: time.Now, Logger: slog.New(slog.NewTextHandler(os.Stderr, nil)), Registerer: prometheus.NewRegistry(), Routes: outbox.Routes{"user.timezone_changed": {"assistant": send}}})
	_ = dispatcher.Run(ctx) // Service-owned worker context cancels at shutdown.
}

type projectedToken struct{}

func (projectedToken) Token(context.Context) (string, error) {
	return "read-current-projected-token", nil
}
func ExampleGRPCDelivery() {
	var conn grpc.ClientConnInterface // Supplied by the service's explicit wiring.
	_, _ = outbox.GRPCDelivery(conn, projectedToken{}, grpc_health_v1.Health_Check_FullMethodName,
		func() proto.Message { return &grpc_health_v1.HealthCheckRequest{} },
		func() proto.Message { return &grpc_health_v1.HealthCheckResponse{} },
	)
	// Use the actual generated usersettings/userdeletion/metrics request and response types.
}
