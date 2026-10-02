// Package resourceslice turns this node's TSNStream CRs into a DRA
// ResourceSlice so the scheduler can place vPLC pods TSN-aware. One stream maps
// to one device; the pool is named after the node.
package resourceslice

import (
	resourceapi "k8s.io/api/resource/v1"
	"k8s.io/dynamic-resource-allocation/resourceslice"

	"tsn.dra.net/dratsn/api/v1alpha1"
)

// Attribute keys (bare names; the domain is the driver name, so a ResourceClaim
// CEL selector reads e.g. device.attributes['tsn.dra.net'].direction).
//
// Only *identity* fields are published (ADR-0022): every DRA attribute is
// selectable by a ResourceClaim CEL, and an allocation must depend only on
// immutable identity so the data-plane fields (vlanId/pcp/dst) can be live-updated
// on a running pod without contradicting the frozen claim (ADR-0021). Those
// data-plane values are not published here — they live in the TSNStream CR (the
// SSOT) and reach the data plane via the checkpoint, not the ResourceSlice.
const (
	AttrStreamID  = "streamId"
	AttrIfName    = "ifName"
	AttrDirection = "direction"
	AttrDomainID  = "domainId"
)

// BuildDriverResources builds the DriverResources for a node from its TSNStreams.
// The slice is always published (even empty) so the driver advertises liveness.
func BuildDriverResources(nodeName string, streams []v1alpha1.TSNStream) resourceslice.DriverResources {
	devices := make([]resourceapi.Device, 0, len(streams))
	for i := range streams {
		devices = append(devices, streamToDevice(&streams[i]))
	}
	return resourceslice.DriverResources{
		Pools: map[string]resourceslice.Pool{
			nodeName: {
				Slices: []resourceslice.Slice{{Devices: devices}},
			},
		},
	}
}

func streamToDevice(s *v1alpha1.TSNStream) resourceapi.Device {
	return resourceapi.Device{
		Name: s.Name,
		Attributes: map[resourceapi.QualifiedName]resourceapi.DeviceAttribute{
			AttrStreamID:  {StringValue: strPtr(s.Spec.StreamID)},
			AttrIfName:    {StringValue: strPtr(s.Spec.IfName)},
			AttrDirection: {StringValue: strPtr(string(s.Spec.Direction))},
			AttrDomainID:  {StringValue: strPtr(s.Spec.DomainID)},
		},
	}
}

func strPtr(s string) *string { return &s }
