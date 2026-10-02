package cnc

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

// Client is the read-only CNC observer interface. The controller depends on this
// abstraction so the mock can be swapped for a real RESTCONF/YANG-push client
// without touching the Stream Mapper.
type Client interface {
	// ListStreams returns the current full set of CNC streams (polling / resync).
	ListStreams(ctx context.Context) ([]Stream, error)
	// Subscribe returns a channel of on-change events (YANG-push). The channel is
	// closed when ctx is done or the connection drops; callers should re-list and
	// re-subscribe (eventually-consistent reconcile handles gaps).
	Subscribe(ctx context.Context) (<-chan Event, error)
}

// listTimeout bounds the data-resource GET. The SSE client deliberately has no
// timeout, so the two cannot share one http.Client: a CNC that accepts the
// connection and then stops answering would otherwise hang a reconcile until
// the context is cancelled.
const listTimeout = 15 * time.Second

// httpClient is the mock CNC client: RESTCONF GET for the full list and an SSE
// event stream for on-change notifications. read-only — never writes.
type httpClient struct {
	baseURL string
	hc      *http.Client // SSE: no timeout
	listHC  *http.Client // data resources: bounded
}

// NewHTTPClient returns a Client talking to the mock CNC at baseURL
// (e.g. http://mock-cnc.kube-system.svc:8080).
func NewHTTPClient(baseURL string) Client {
	return &httpClient{
		baseURL: strings.TrimRight(baseURL, "/"),
		hc:      &http.Client{Timeout: 0}, // no timeout: SSE is long-lived
		listHC:  &http.Client{Timeout: listTimeout},
	}
}

func (c *httpClient) ListStreams(ctx context.Context) ([]Stream, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.baseURL+PathStreams, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Accept", MediaTypeYANGJSON)
	resp, err := c.listHC.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("list streams: %s", describeError(resp))
	}
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("read streams: %w", err)
	}
	return decodeStreams(body)
}

// decodeStreams accepts the RESTCONF container form and the bare array an older
// mock returns, so a controller can be rolled out before its CNC.
func decodeStreams(body []byte) ([]Stream, error) {
	var list StreamList
	if err := json.Unmarshal(body, &list); err == nil && list.Streams.Stream != nil {
		return list.Streams.Stream, nil
	}
	var streams []Stream
	if err := json.Unmarshal(body, &streams); err != nil {
		return nil, fmt.Errorf("decode streams: %w", err)
	}
	return streams, nil
}

// describeError turns an ietf-restconf:errors body (RFC 8040 §7.1) into a log
// line. Without this the controller only ever reports a bare status code, which
// is the one thing the standard error body exists to improve on.
func describeError(resp *http.Response) string {
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 8<<10))
	var errs Errors
	if json.Unmarshal(body, &errs) == nil && len(errs.Errors.Error) > 0 {
		e := errs.Errors.Error[0]
		msg := fmt.Sprintf("status %d (%s/%s", resp.StatusCode, e.Type, e.Tag)
		if e.Message != "" {
			msg += ": " + e.Message
		}
		return msg + ")"
	}
	return fmt.Sprintf("status %d", resp.StatusCode)
}

func (c *httpClient) Subscribe(ctx context.Context) (<-chan Event, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.baseURL+PathNotifications, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Accept", "text/event-stream")
	resp, err := c.hc.Do(req)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode != http.StatusOK {
		defer resp.Body.Close()
		return nil, fmt.Errorf("subscribe: %s", describeError(resp))
	}

	out := make(chan Event)
	go func() {
		defer close(out)
		defer resp.Body.Close()
		sc := bufio.NewScanner(resp.Body)
		sc.Buffer(make([]byte, 0, 64*1024), 1<<20)
		for sc.Scan() {
			line := sc.Text()
			data, ok := strings.CutPrefix(line, "data: ")
			if !ok {
				continue
			}
			ev, ok := decodeNotification([]byte(data))
			if !ok {
				continue
			}
			select {
			case out <- ev:
			case <-ctx.Done():
				return
			}
		}
	}()
	return out, nil
}

// DialWait polls ListStreams until the CNC is reachable or ctx is done. Useful
// at controller startup since the mock CNC may come up concurrently.
func DialWait(ctx context.Context, c Client) error {
	for {
		if _, err := c.ListStreams(ctx); err == nil {
			return nil
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(time.Second):
		}
	}
}

// decodeNotification unwraps the RFC 8040 §6.4 envelope. A bare event object
// (the pre-audit shape) is still accepted so the two sides can be upgraded
// independently; either way the change-type is normalized.
func decodeNotification(data []byte) (Event, bool) {
	var n Notification
	if err := json.Unmarshal(data, &n); err == nil && n.Notification.Change.Stream.StreamID != "" {
		ev := n.Notification.Change
		ev.Type = ev.Type.Normalize()
		return ev, true
	}
	var ev Event
	if err := json.Unmarshal(data, &ev); err != nil {
		return Event{}, false
	}
	// the pre-audit payload spelled the member "type"
	var legacy struct {
		Type EventType `json:"type"`
	}
	if ev.Type == "" && json.Unmarshal(data, &legacy) == nil {
		ev.Type = legacy.Type
	}
	ev.Type = ev.Type.Normalize()
	return ev, ev.Stream.StreamID != ""
}
