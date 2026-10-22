package outbox

import (
	"context"
	"errors"
	"strings"

	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"
	"google.golang.org/protobuf/proto"
)

// TokenSource supplies the current ServiceAccount bearer token for finassist-internal.
// The service wires its identity library here; token rotation stays outside outbox.
type TokenSource interface {
	Token(context.Context) (string, error)
}

// GRPCDelivery builds a system unary RPC delivery. The service supplies its cluster transport (HTTP/2 inside WireGuard per the contract);
// tokens must have audience finassist-internal and factories must return fresh messages.
// Payload is unmarshaled into request; key must be part of the producer's protobuf request.
// It sends only x-service-authorization metadata, discarding incoming user/client metadata.
// Errors never contain a token, payload or the peer's potentially sensitive error text.
func GRPCDelivery(conn grpc.ClientConnInterface, tokens TokenSource, method string, request, reply func() proto.Message) (Delivery, error) {
	if conn == nil || tokens == nil || !strings.HasPrefix(method, "/") || strings.Count(method, "/") != 2 || request == nil || reply == nil {
		return nil, errors.New("outbox: invalid gRPC delivery")
	}
	return func(ctx context.Context, event Event) error {
		req, res := request(), reply()
		if req == nil || res == nil {
			return errors.New("outbox: message factory returned nil")
		}
		if err := proto.Unmarshal(event.Payload, req); err != nil {
			return errors.New("outbox: invalid protobuf payload")
		}
		token, err := tokens.Token(ctx)
		if err != nil || token == "" || strings.ContainsAny(token, "\r\n") {
			return errors.New("outbox: service token unavailable")
		}
		ctx = metadata.NewOutgoingContext(ctx, metadata.Pairs("x-service-authorization", "Bearer "+token))
		if err = conn.Invoke(ctx, method, req, res); err != nil {
			return errors.New("outbox: system RPC failed")
		}
		return nil
	}, nil
}
