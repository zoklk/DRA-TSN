// mock-cnc is a stand-in for the real CNC: it serves the current TSN stream set
// over a RESTCONF-shaped HTTP endpoint and pushes on-change notifications over
// SSE (YANG-push stub). An admin API drives the stream lifecycle in tests.
//
// Endpoints:
//
//	GET    /restconf/data/dratsn-cnc:streams -> {"dratsn-cnc:streams":{"stream":[…]}}
//	GET    /restconf/data/tsn-streams       -> []cnc.Stream (deprecated alias)
//	GET    /restconf/data/topology          -> topology stub
//	GET    /restconf/streams/dratsn-cnc:stream-change -> SSE, RFC 8040 §6.4 envelope
//	GET    /restconf/notifications          -> same stream (deprecated alias)
//	POST   /admin/streams                   -> add/update a stream (body: cnc.Stream)
//	DELETE /admin/streams/{streamId}        -> remove a stream
//	GET    /healthz
package main

import (
	"encoding/json"
	"flag"
	"net/http"
	"strings"
	"sync"
	"time"

	"k8s.io/klog/v2"

	"tsn.dra.net/dratsn/pkg/cnc"
)

type store struct {
	mu      sync.RWMutex
	streams map[string]cnc.Stream
	subs    map[chan cnc.Event]struct{}
}

func newStore() *store {
	return &store{
		streams: map[string]cnc.Stream{},
		subs:    map[chan cnc.Event]struct{}{},
	}
}

func (s *store) list() []cnc.Stream {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make([]cnc.Stream, 0, len(s.streams))
	for _, st := range s.streams {
		out = append(out, st)
	}
	return out
}

func (s *store) put(st cnc.Stream) {
	s.mu.Lock()
	s.streams[st.StreamID] = st
	s.mu.Unlock()
	s.broadcast(cnc.Event{Type: cnc.EventCreate, Stream: st})
}

func (s *store) delete(id string) bool {
	s.mu.Lock()
	st, ok := s.streams[id]
	delete(s.streams, id)
	s.mu.Unlock()
	if ok {
		s.broadcast(cnc.Event{Type: cnc.EventDelete, Stream: st})
	}
	return ok
}

func (s *store) broadcast(ev cnc.Event) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	for ch := range s.subs {
		select {
		case ch <- ev:
		default: // slow subscriber: drop; it will resync via ListStreams
		}
	}
}

func (s *store) subscribe() chan cnc.Event {
	ch := make(chan cnc.Event, 64)
	s.mu.Lock()
	s.subs[ch] = struct{}{}
	s.mu.Unlock()
	return ch
}

func (s *store) unsubscribe(ch chan cnc.Event) {
	s.mu.Lock()
	delete(s.subs, ch)
	s.mu.Unlock()
}

func main() {
	klog.InitFlags(nil)
	addr := flag.String("addr", ":8080", "listen address")
	flag.Parse()

	st := newStore()
	mux := http.NewServeMux()

	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	})

	// RESTCONF data resources. The module-qualified path is the standard form
	// (RFC 8040 §3.5.3) and returns the container; the unqualified path is kept
	// as a deprecated alias returning the bare array, because collection
	// scripts (experiments/lib.sh, fig_mixed/run.sh) parse that shape.
	mux.HandleFunc("GET "+cnc.PathStreams, func(w http.ResponseWriter, _ *http.Request) {
		var list cnc.StreamList
		list.Streams.Stream = st.list()
		writeYANG(w, list)
	})

	mux.HandleFunc("GET "+cnc.LegacyPathStreams, func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, st.list())
	})

	topology := func(w http.ResponseWriter, _ *http.Request) {
		// Minimal topology stub: the mock joins purely by talker/listener MAC/IP,
		// so the topology document is informational for now.
		writeYANG(w, map[string]any{cnc.ModuleName + ":topology": map[string]any{
			"node": []any{}, "link": []any{}}})
	}
	mux.HandleFunc("GET "+cnc.PathTopology, topology)
	mux.HandleFunc("GET "+cnc.LegacyPathTopology, topology)

	notifications := func(w http.ResponseWriter, r *http.Request) {
		flusher, ok := w.(http.Flusher)
		if !ok {
			http.Error(w, "streaming unsupported", http.StatusInternalServerError)
			return
		}
		w.Header().Set("Content-Type", "text/event-stream")
		w.Header().Set("Cache-Control", "no-cache")
		w.Header().Set("Connection", "keep-alive")
		w.WriteHeader(http.StatusOK)
		flusher.Flush()

		ch := st.subscribe()
		defer st.unsubscribe(ch)
		klog.V(2).Info("SSE subscriber connected")

		enc := json.NewEncoder(w)
		for {
			select {
			case <-r.Context().Done():
				return
			case ev := <-ch:
				var n cnc.Notification
				n.Notification.EventTime = time.Now().UTC().Format(time.RFC3339)
				n.Notification.Change = ev
				if _, err := w.Write([]byte("data: ")); err != nil {
					return
				}
				// Encode writes one trailing LF; SSE dispatches an event on a
				// BLANK line, so the second LF is what ends the event. Without
				// it a conformant EventSource accumulates every notification
				// into one never-dispatched event.
				if err := enc.Encode(n); err != nil {
					return
				}
				if _, err := w.Write([]byte("\n")); err != nil {
					return
				}
				flusher.Flush()
			}
		}
	}
	mux.HandleFunc("GET "+cnc.PathNotifications, notifications)
	mux.HandleFunc("GET "+cnc.LegacyPathNotifications, notifications)

	mux.HandleFunc("POST /admin/streams", func(w http.ResponseWriter, r *http.Request) {
		var s cnc.Stream
		if err := json.NewDecoder(r.Body).Decode(&s); err != nil || s.StreamID == "" {
			writeError(w, http.StatusBadRequest, "protocol", "malformed-message",
				"stream body is not decodable or has no streamId")
			return
		}
		st.put(s)
		klog.Infof("admin: put stream %s (vlan %d pcp %d domain %s)", s.StreamID, s.VLANID, s.PCP, s.DomainID)
		w.WriteHeader(http.StatusCreated)
	})

	mux.HandleFunc("DELETE /admin/streams/{streamId}", func(w http.ResponseWriter, r *http.Request) {
		id := strings.TrimSpace(r.PathValue("streamId"))
		if st.delete(id) {
			klog.Infof("admin: deleted stream %s", id)
			w.WriteHeader(http.StatusNoContent)
			return
		}
		writeError(w, http.StatusNotFound, "application", "data-missing",
			"no stream with that id")
	})

	klog.Infof("mock-cnc listening on %s", *addr)
	srv := &http.Server{Addr: *addr, Handler: mux}
	if err := srv.ListenAndServe(); err != nil {
		klog.Fatalf("server error: %v", err)
	}
}

func writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(v)
}

// writeYANG answers a RESTCONF data resource: the JSON media type the standard
// defines for them (RFC 8040 §3.2).
func writeYANG(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", cnc.MediaTypeYANGJSON)
	_ = json.NewEncoder(w).Encode(v)
}

// writeError answers with an ietf-restconf:errors body (RFC 8040 §7.1) instead
// of a plain-text line, so a client can act on error-tag rather than on prose.
func writeError(w http.ResponseWriter, status int, etype, tag, msg string) {
	var e cnc.Errors
	e.Errors.Error = []cnc.ErrorEntry{{Type: etype, Tag: tag, Message: msg}}
	w.Header().Set("Content-Type", cnc.MediaTypeYANGJSON)
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(e)
}
