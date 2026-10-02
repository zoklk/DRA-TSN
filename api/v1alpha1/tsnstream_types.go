package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// StreamDirection is the data-plane direction of a TSN stream relative to the
// node/NIC it was joined to.
type StreamDirection string

const (
	// DirectionTX: the node/NIC is the talker side (vPLC -> robot). dst = external listener.
	DirectionTX StreamDirection = "TX"
	// DirectionRX: the node/NIC is the listener side (sensor -> vPLC). dst = this node's TSN NIC endpoint.
	DirectionRX StreamDirection = "RX"
)

// Endpoint identifies a TSN talker/listener endpoint.
type Endpoint struct {
	IP  string `json:"ip,omitempty"`
	MAC string `json:"mac,omitempty"`
}

// Dst is the destination of a stream's packets.
//
//   - TX: dst is the external listener (robot arm) address.
//   - RX: dst is this node's TSN NIC host endpoint (IP:port) — the static
//     receive point a sensor targets, defined by CUC/network design (not a pod IP).
type Dst struct {
	IP   string `json:"ip"`
	Port int32  `json:"port"`
	MAC  string `json:"mac,omitempty"`
}

// TSNStreamSpec is the result of joining a CNC stream with a node/NIC.
type TSNStreamSpec struct {
	// StreamID is the CNC stream identifier.
	StreamID string `json:"streamId"`
	// NodeName / IfName identify the joined NIC.
	NodeName string `json:"nodeName"`
	IfName   string `json:"ifName"`
	// Direction is TX or RX.
	Direction StreamDirection `json:"direction"`
	// Dst is the stream destination (see Dst doc for TX/RX semantics).
	Dst Dst `json:"dst"`
	// VLANID is the 802.1Q VLAN id (0-4094).
	VLANID int32 `json:"vlanId"`
	// PCP is the 802.1p priority (0-7).
	PCP int32 `json:"pcp"`
	// DomainID groups streams of a TSN domain/cell (cf. 802.1Qcc TSN domain).
	DomainID string `json:"domainId"`
	// Talker / Listener carry optional endpoint info from the CNC.
	Talker   *Endpoint `json:"talker,omitempty"`
	Listener *Endpoint `json:"listener,omitempty"`
	// MaxLatencyNs / AccumulatedLatencyNs mirror the CNC's 802.1Qcc latency
	// figures (ns). MaxLatencyNs == 0 means "not provided" — such streams are
	// ignored by the TSN-slack scheduler score. Scheduler-only data: never
	// published as a ResourceSlice attribute (ADR-0022) and never consumed by
	// the data plane.
	MaxLatencyNs         int64 `json:"maxLatencyNs,omitempty"`
	AccumulatedLatencyNs int64 `json:"accumulatedLatencyNs,omitempty"`
}

// TSNStreamStatus holds observed stream status.
type TSNStreamStatus struct {
	ObservedGeneration int64              `json:"observedGeneration,omitempty"`
	Conditions         []metav1.Condition `json:"conditions,omitempty"`
}

// +k8s:deepcopy-gen:interfaces=k8s.io/apimachinery/pkg/runtime.Object

// TSNStream is a cluster-scoped CNC-stream-joined-to-node/NIC record published
// by the TSN controller and consumed by the DRA plugin.
type TSNStream struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   TSNStreamSpec   `json:"spec,omitempty"`
	Status TSNStreamStatus `json:"status,omitempty"`
}

// +k8s:deepcopy-gen:interfaces=k8s.io/apimachinery/pkg/runtime.Object

// TSNStreamList is a list of TSNStream.
type TSNStreamList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []TSNStream `json:"items"`
}
