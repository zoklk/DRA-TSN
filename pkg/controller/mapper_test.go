package controller

import (
	"testing"

	"tsn.dra.net/dratsn/api/v1alpha1"
	"tsn.dra.net/dratsn/pkg/cnc"
)

func nic(node, ifn, mac, ip string) v1alpha1.TSNNic {
	return v1alpha1.TSNNic{
		Spec: v1alpha1.TSNNicSpec{
			NodeName: node, IfName: ifn, MACAddress: mac, HostIP: ip, TSNCapable: true,
		},
	}
}

// A talker-matching NIC yields a TX device (dst = external listener); a
// listener-matching NIC yields an RX device (dst = that node's NIC endpoint).
func TestDesiredStreamsTXandRX(t *testing.T) {
	nics := []v1alpha1.TSNNic{
		nic("alpha-w1", "eno6", "0c:c4:7a:9f:f1:ad", "192.168.10.12"),
	}
	streams := []cnc.Stream{
		{ // talker = w1 NIC -> TX
			StreamID: "s-tx",
			Talker:   cnc.Endpoint{MAC: "0c:c4:7a:9f:f1:ad"},
			Listener: cnc.Endpoint{IP: "192.168.50.10", MAC: "02:00:00:00:50:10"},
			Port:     4840, VLANID: 200, PCP: 6, DomainID: "cell-A",
			MaxLatency: 1000000, AccumulatedLatency: 250000,
		},
		{ // listener = w1 NIC IP -> RX
			StreamID: "s-rx",
			Talker:   cnc.Endpoint{IP: "192.168.60.20"},
			Listener: cnc.Endpoint{IP: "192.168.10.12"},
			Port:     4840, VLANID: 100, PCP: 5, DomainID: "cell-A",
		},
	}

	got := DesiredStreams(streams, nics)
	if len(got) != 2 {
		t.Fatalf("want 2 streams, got %d: %v", len(got), keys(got))
	}

	var tx, rx *v1alpha1.TSNStream
	for name := range got {
		s := got[name]
		switch s.Spec.Direction {
		case v1alpha1.DirectionTX:
			tx = ptr(got, name)
		case v1alpha1.DirectionRX:
			rx = ptr(got, name)
		}
	}
	if tx == nil || rx == nil {
		t.Fatalf("missing a direction: tx=%v rx=%v", tx, rx)
	}

	// TX dst = external listener.
	if tx.Spec.Dst.IP != "192.168.50.10" || tx.Spec.Dst.Port != 4840 || tx.Spec.Dst.MAC != "02:00:00:00:50:10" {
		t.Errorf("TX dst = %+v, want listener 192.168.50.10:4840", tx.Spec.Dst)
	}
	if tx.Spec.DomainID != "cell-A" || tx.Spec.VLANID != 200 {
		t.Errorf("TX domain/vlan = %s/%d", tx.Spec.DomainID, tx.Spec.VLANID)
	}
	// RX dst = this node's NIC endpoint (static receive point).
	if rx.Spec.Dst.IP != "192.168.10.12" || rx.Spec.Dst.MAC != "0c:c4:7a:9f:f1:ad" {
		t.Errorf("RX dst = %+v, want node NIC endpoint", rx.Spec.Dst)
	}
	// 802.1Qcc latency figures flow through into the spec (ns).
	if tx.Spec.MaxLatencyNs != 1000000 || tx.Spec.AccumulatedLatencyNs != 250000 {
		t.Errorf("TX latency = max %d / acc %d, want 1000000/250000",
			tx.Spec.MaxLatencyNs, tx.Spec.AccumulatedLatencyNs)
	}
}

// A CNC accumulated-latency change alone must be detected as a spec change,
// or path recomputation by the CNC never reaches the CR (same failure mode
// specEqual once had for talker/listener).
func TestSpecEqualLatency(t *testing.T) {
	a := v1alpha1.TSNStreamSpec{StreamID: "s", MaxLatencyNs: 1000000, AccumulatedLatencyNs: 250000}
	b := a
	if !specEqual(a, b) {
		t.Fatalf("identical specs compared unequal")
	}
	b.AccumulatedLatencyNs = 400000
	if specEqual(a, b) {
		t.Errorf("accumulatedLatencyNs change not detected by specEqual")
	}
	b = a
	b.MaxLatencyNs = 2000000
	if specEqual(a, b) {
		t.Errorf("maxLatencyNs change not detected by specEqual")
	}
}

// A NIC matching neither endpoint produces no stream; a non-TSN NIC is skipped.
func TestDesiredStreamsNoMatch(t *testing.T) {
	nics := []v1alpha1.TSNNic{nic("alpha-w2", "eno6", "aa:aa:aa:aa:aa:aa", "10.0.0.9")}
	streams := []cnc.Stream{{
		StreamID: "s", Talker: cnc.Endpoint{MAC: "0c:c4:7a:9f:f1:ad"},
		Listener: cnc.Endpoint{IP: "192.168.50.10"}, Port: 4840, DomainID: "cell-A",
	}}
	if got := DesiredStreams(streams, nics); len(got) != 0 {
		t.Errorf("want 0 streams for non-matching NIC, got %d", len(got))
	}

	notTSN := nics[0]
	notTSN.Spec.TSNCapable = false
	matchStream := []cnc.Stream{{StreamID: "s", Talker: cnc.Endpoint{MAC: "aa:aa:aa:aa:aa:aa"}, Port: 4840}}
	if got := DesiredStreams(matchStream, []v1alpha1.TSNNic{notTSN}); len(got) != 0 {
		t.Errorf("non-TSN NIC must be skipped, got %d streams", len(got))
	}
}

func TestEndpointMatchesNIC(t *testing.T) {
	n := nic("w1", "eno6", "0C:C4:7A:9F:F1:AD", "192.168.10.12")
	// MAC match is case-insensitive.
	if !endpointMatchesNIC(cnc.Endpoint{MAC: "0c:c4:7a:9f:f1:ad"}, n) {
		t.Errorf("MAC match (case-insensitive) failed")
	}
	if !endpointMatchesNIC(cnc.Endpoint{IP: "192.168.10.12"}, n) {
		t.Errorf("IP match failed")
	}
	if endpointMatchesNIC(cnc.Endpoint{IP: "192.168.10.99"}, n) {
		t.Errorf("non-matching IP matched")
	}
	// Empty endpoint must not match (avoid matching every NIC).
	if endpointMatchesNIC(cnc.Endpoint{}, n) {
		t.Errorf("empty endpoint matched")
	}
}

func TestStreamNameSanitizeAndIdentity(t *testing.T) {
	name := streamName("s-tx:01", "alpha-w1", "eno6", v1alpha1.DirectionTX)
	for _, r := range name {
		ok := (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9') || r == '-'
		if !ok {
			t.Fatalf("name %q has invalid char %q (must be DNS-1123)", name, r)
		}
	}
	// Node + ifName + direction are part of the name => a node/dir change is a
	// different CR (so an identity change is never an in-place CR update).
	other := streamName("s-tx:01", "alpha-w2", "eno6", v1alpha1.DirectionTX)
	if name == other {
		t.Errorf("name must differ when node differs: %q == %q", name, other)
	}
}

func keys(m map[string]v1alpha1.TSNStream) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}

func ptr(m map[string]v1alpha1.TSNStream, k string) *v1alpha1.TSNStream {
	s := m[k]
	return &s
}
