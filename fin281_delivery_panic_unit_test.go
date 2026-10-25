package outbox

import (
	"bytes"
	"context"
	"errors"
	"log/slog"
	"runtime"
	"strings"
	"testing"

	"github.com/prometheus/client_golang/prometheus"
)

func TestUnitDeliverRecoversOnlyPanics(t *testing.T) {
	peer := errors.New("peer failure")
	for _, outcome := range []string{"success", "error", "panic", "nil panic"} {
		t.Run(outcome, func(t *testing.T) {
			err := deliver(t.Context(), func(context.Context, Event) error {
				switch outcome {
				case "error":
					return peer
				case "panic":
					panic("PRIVATE payload")
				case "nil panic":
					panic(nil) // Go 1.21+ turns this into *runtime.PanicNilError; it is still a panic.
				}
				return nil
			}, Event{})
			switch outcome {
			case "success":
				if err != nil {
					t.Fatal(err)
				}
			case "error":
				if !errors.Is(err, peer) {
					t.Fatalf("delivery error replaced: %v", err)
				}
			default:
				if !errors.Is(err, errDeliveryPanic) || strings.Contains(err.Error(), "PRIVATE") {
					t.Fatalf("panic not classified safely: %v", err)
				}
			}
		})
	}
	t.Run("goexit", func(t *testing.T) {
		returned := make(chan struct{})
		done := make(chan struct{})
		go func() {
			defer close(done)
			_ = deliver(t.Context(), func(context.Context, Event) error { runtime.Goexit(); return nil }, Event{})
			close(returned)
		}()
		<-done
		select {
		case <-returned:
			t.Fatal("runtime.Goexit was swallowed")
		default:
		}
	})
}

func TestUnitDispatcherPanickingDeliveryIsRetriedSafely(t *testing.T) {
	tx := &fin244Tx{target: "x"}
	session := &fin244Session{txs: []*fin244Tx{tx}}
	others := 0
	d := fin244Dispatcher(func(context.Context, Event) error { panic("PRIVATE financial payload") })
	d.options.Routes["t"]["y"] = func(context.Context, Event) error { others++; return nil }
	var logs bytes.Buffer
	d.options.Logger = slog.New(slog.NewTextHandler(&logs, nil))
	lock := false
	found, err := d.dispatch(t.Context(), session, "x", &lock)
	if !found || !lock || !errors.Is(err, errRetryScheduled) {
		t.Fatalf("panic was not a scheduled retry: found=%v lock=%v err=%v", found, lock, err)
	}
	if tx.commits != 1 || session.execs != 1 || !strings.HasPrefix(session.lastSQL, "UPDATE") || len(session.lastArgs) != 3 || session.lastArgs[2] != "delivery_failed" {
		t.Fatalf("retry not persisted with bounded classification: sql=%q args=%v", session.lastSQL, session.lastArgs)
	}
	registry := prometheus.NewRegistry()
	registry.MustRegister(d.failures)
	families, gatherErr := registry.Gather()
	if gatherErr != nil || len(families) != 1 || len(families[0].GetMetric()) != 1 || families[0].GetMetric()[0].GetCounter().GetValue() != 1 || families[0].GetMetric()[0].GetLabel()[0].GetValue() != "x" {
		t.Fatalf("failure metric not counted for the target: %v %v", families, gatherErr)
	}
	if others != 0 {
		t.Fatal("another target's delivery ran for this row")
	}
	if !strings.Contains(logs.String(), "outbox delivery panicked") || !strings.Contains(logs.String(), "target=x") || strings.Contains(logs.String(), "PRIVATE") {
		t.Fatalf("unsafe or missing panic log: %q", logs.String())
	}
}
