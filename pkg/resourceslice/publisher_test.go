package resourceslice

import (
	"testing"

	resourceapi "k8s.io/api/resource/v1"

	"tsn.dra.net/dratsn/api/v1alpha1"
)

func sampleStream() v1alpha1.TSNStream {
	s := v1alpha1.TSNStream{}
	s.Name = "s-tx-alpha-w1-eno6-tx"
	s.Spec.StreamID = "s-tx"
	s.Spec.IfName = "eno6"
	s.Spec.Direction = v1alpha1.DirectionTX
	s.Spec.DomainID = "cell-A"
	s.Spec.VLANID = 200
	s.Spec.PCP = 6
	s.Spec.Dst = v1alpha1.Dst{IP: "192.168.50.10", Port: 4840}
	return s
}

// ADR-0022: ResourceSlice must publish only immutable identity attributes. Data-
// plane fields (vlanId/pcp/dstIp/dstPort) must NOT be selectable, or a selector
// could bind an allocation to a value that live-update later changes.
func TestDeviceAttributesIdentityOnly(t *testing.T) {
	s := sampleStream()
	dev := streamToDevice(&s)

	want := map[string]bool{
		AttrStreamID: true, AttrIfName: true, AttrDirection: true, AttrDomainID: true,
	}
	for k := range dev.Attributes {
		if !want[string(k)] {
			t.Errorf("unexpected attribute %q published (only identity allowed, ADR-0022)", k)
		}
		delete(want, string(k))
	}
	if len(want) != 0 {
		t.Errorf("missing identity attributes: %v", want)
	}

	// Explicitly assert the data-plane keys are absent.
	for _, banned := range []string{"vlanId", "pcp", "dstIp", "dstPort"} {
		if _, ok := dev.Attributes[resourceapi.QualifiedName(banned)]; ok {
			t.Errorf("data-plane attribute %q must not be published", banned)
		}
	}

	if dev.Name != s.Name {
		t.Errorf("device name = %q, want %q", dev.Name, s.Name)
	}
	if v := dev.Attributes[AttrDirection].StringValue; v == nil || *v != "TX" {
		t.Errorf("direction attribute = %v, want TX", v)
	}
}

func TestBuildDriverResourcesPoolPerNode(t *testing.T) {
	s := sampleStream()
	res := BuildDriverResources("alpha-w1", []v1alpha1.TSNStream{s})
	pool, ok := res.Pools["alpha-w1"]
	if !ok {
		t.Fatalf("no pool for node alpha-w1: %v", res.Pools)
	}
	if len(pool.Slices) != 1 || len(pool.Slices[0].Devices) != 1 {
		t.Fatalf("want 1 slice/1 device, got %+v", pool.Slices)
	}
	// Empty stream list still publishes a (liveness) pool.
	empty := BuildDriverResources("alpha-w3", nil)
	if _, ok := empty.Pools["alpha-w3"]; !ok {
		t.Errorf("empty node must still publish a pool (liveness)")
	}
}
