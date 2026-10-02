package cnc

import "testing"

// The controller and the mock CNC are deployed separately, so the two wire
// shapes have to coexist: the RESTCONF container/envelope form (RFC 8040
// §3.5.3, §6.4) and the pre-audit bare forms. These tests pin both directions.

func TestDecodeStreams(t *testing.T) {
	cases := map[string]string{
		"restconf container": `{"dratsn-cnc:streams":{"stream":[{"streamId":"s-tx","vlanId":100}]}}`,
		"legacy bare array":  `[{"streamId":"s-tx","vlanId":100}]`,
	}
	for name, body := range cases {
		t.Run(name, func(t *testing.T) {
			got, err := decodeStreams([]byte(body))
			if err != nil {
				t.Fatalf("decode: %v", err)
			}
			if len(got) != 1 || got[0].StreamID != "s-tx" || got[0].VLANID != 100 {
				t.Fatalf("got %+v", got)
			}
		})
	}
}

func TestDecodeNotification(t *testing.T) {
	cases := []struct {
		name string
		body string
		want EventType
	}{
		{
			name: "restconf envelope",
			body: `{"ietf-restconf:notification":{"eventTime":"2026-10-01T00:00:00Z",` +
				`"dratsn-cnc:stream-change":{"change-type":"create","stream":{"streamId":"s-tx"}}}}`,
			want: EventCreate,
		},
		{
			name: "restconf envelope delete",
			body: `{"ietf-restconf:notification":{"eventTime":"2026-10-01T00:00:00Z",` +
				`"dratsn-cnc:stream-change":{"change-type":"delete","stream":{"streamId":"s-tx"}}}}`,
			want: EventDelete,
		},
		{
			name: "legacy bare event is normalized",
			body: `{"type":"added","stream":{"streamId":"s-tx"}}`,
			want: EventCreate,
		},
		{
			name: "legacy delete is normalized",
			body: `{"type":"deleted","stream":{"streamId":"s-tx"}}`,
			want: EventDelete,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			ev, ok := decodeNotification([]byte(tc.body))
			if !ok {
				t.Fatal("not decoded")
			}
			if ev.Type != tc.want {
				t.Fatalf("change-type: got %q want %q", ev.Type, tc.want)
			}
			if ev.Stream.StreamID != "s-tx" {
				t.Fatalf("stream: got %q", ev.Stream.StreamID)
			}
		})
	}
}

func TestDecodeNotificationRejectsGarbage(t *testing.T) {
	for _, body := range []string{`{}`, `{"ietf-restconf:notification":{}}`, `not json`} {
		if _, ok := decodeNotification([]byte(body)); ok {
			t.Fatalf("accepted %q", body)
		}
	}
}
