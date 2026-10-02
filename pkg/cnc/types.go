// Package cnc models the (mock) CNC stream/topology data and the read-only
// client the TSN controller uses to observe it.
//
// Standards position (audited 2026-10-01). The wire format follows RESTCONF
// (RFC 8040): module-qualified data paths (§3.5.3), application/yang-data+json
// (§3.2), the "ietf-restconf:notification" envelope with eventTime (§6.4) over
// an SSE event stream (§6.3), and ietf-restconf:errors bodies (§7.1). What is
// NOT implemented: the RFC 8650 dynamic subscription RPC
// (establish-subscription) and the RFC 8641 push-update/push-change-update
// datastore subscription. RFC 8040 §6.1 makes notification support optional, so
// the §6.3 shape is a documented subset rather than a claim of YANG-push
// conformance. Field names and units follow ieee802-dot1q-tsn-types
// (max-latency / accumulated-latency, uint32 ns).
package cnc

// ModuleName is the YANG module the mock CNC presents. RESTCONF requires the
// first element after /data/ to be module-qualified (RFC 8040 §3.5.3), so every
// resource path is built from this name.
const ModuleName = "dratsn-cnc"

// MediaTypeYANGJSON is the RESTCONF JSON media type (RFC 8040 §3.2, §11.3.2).
const MediaTypeYANGJSON = "application/yang-data+json"

// Endpoint is a TSN talker or listener endpoint as seen by the CNC. The MAC/IP
// are what the Stream Mapper joins against node NIC inventory (TSNNic).
type Endpoint struct {
	IP  string `json:"ip,omitempty"`
	MAC string `json:"mac,omitempty"`
}

// Stream is a single CNC stream record. It is the source of truth for the IP
// stream identification fields (dst, vlan, pcp) that flow into TSNStream CRs and
// ultimately bpf maps / pod env.
//
// Direction is not stored here: it is derived per node by the Stream Mapper from
// whether a node's NIC matches the talker (TX) or listener (RX) endpoint.
type Stream struct {
	StreamID string   `json:"streamId"`
	Talker   Endpoint `json:"talker"`
	Listener Endpoint `json:"listener"`
	Port     int32    `json:"port"`
	VLANID   int32    `json:"vlanId"`
	PCP      int32    `json:"pcp"`
	DomainID string   `json:"domainId"`
	// MaxLatency / AccumulatedLatency are the 802.1Qcc latency figures in ns
	// (user-to-network-requirements/max-latency, group-status-talker-listener/
	// accumulated-latency; both uint32 per ieee802-dot1q-tsn-types). The real
	// CNC computes AccumulatedLatency per path; the mock declares it per
	// stream. 0 = not provided.
	MaxLatency         uint32 `json:"maxLatency,omitempty"`
	AccumulatedLatency uint32 `json:"accumulatedLatency,omitempty"`
}

// EventType is the kind of on-change notification pushed over SSE. The values
// are the RFC 8641 §3.7 change-type enumeration (only the two a read-only
// stream inventory can produce).
type EventType string

const (
	EventCreate EventType = "create"
	EventDelete EventType = "delete"

	// Pre-audit spellings. Kept so a controller talking to an older mock, or a
	// mock talking to an older controller, still agrees on the meaning.
	legacyAdded   EventType = "added"
	legacyDeleted EventType = "deleted"
)

// Normalize maps the legacy spellings onto the RFC 8641 change-type values.
func (t EventType) Normalize() EventType {
	switch t {
	case legacyAdded:
		return EventCreate
	case legacyDeleted:
		return EventDelete
	}
	return t
}

// Event is one on-change notification about a stream.
type Event struct {
	Type   EventType `json:"change-type"`
	Stream Stream    `json:"stream"`
}

// Notification is the RFC 8040 §6.4 envelope carrying an Event. eventTime is
// mandatory in that envelope; the payload member is module-qualified.
type Notification struct {
	Notification struct {
		EventTime string `json:"eventTime"`
		Change    Event  `json:"dratsn-cnc:stream-change"`
	} `json:"ietf-restconf:notification"`
}

// StreamList is the RESTCONF representation of the stream collection: a
// module-qualified container holding the YANG list (RFC 8040 §3.5.3).
type StreamList struct {
	Streams struct {
		Stream []Stream `json:"stream"`
	} `json:"dratsn-cnc:streams"`
}

// RESTCONF resource paths. The legacy, unqualified paths stay served by the
// mock so collection scripts written against them keep working.
const (
	PathStreams       = "/restconf/data/" + ModuleName + ":streams"
	PathTopology      = "/restconf/data/" + ModuleName + ":topology"
	PathNotifications = "/restconf/streams/" + ModuleName + ":stream-change"

	LegacyPathStreams       = "/restconf/data/tsn-streams"
	LegacyPathTopology      = "/restconf/data/topology"
	LegacyPathNotifications = "/restconf/notifications"
)

// Errors is the RFC 8040 §7.1 error response body.
type Errors struct {
	Errors struct {
		Error []ErrorEntry `json:"error"`
	} `json:"ietf-restconf:errors"`
}

// ErrorEntry is one entry of an ietf-restconf:errors body.
type ErrorEntry struct {
	Type    string `json:"error-type"`
	Tag     string `json:"error-tag"`
	Path    string `json:"error-path,omitempty"`
	Message string `json:"error-message,omitempty"`
}
