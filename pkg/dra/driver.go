// Package dra implements the kubeletplugin.DRAPlugin handlers. On
// NodePrepareResources it resolves each allocated device (= a TSNStream) to a
// StreamConfig and writes a pod->stream checkpoint that the NRI hooks consume at
// pod start. env injection and bpf programming happen in NRI (Phase 3), not here.
package dra

import (
	"context"
	"fmt"

	resourceapi "k8s.io/api/resource/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/dynamic-resource-allocation/kubeletplugin"
	"k8s.io/klog/v2"

	"tsn.dra.net/dratsn/api/v1alpha1"
	"tsn.dra.net/dratsn/pkg/checkpoint"
)

// StreamGetter resolves an allocated device name to its TSNStream spec. Backed
// by the cached controller-runtime client over this node's TSNStreams.
type StreamGetter func(ctx context.Context, name string) (*v1alpha1.TSNStream, error)

// Driver implements kubeletplugin.DRAPlugin for the TSN driver.
type Driver struct {
	DriverName string
	NodeName   string
	Store      *checkpoint.Store
	GetStream  StreamGetter
}

// PrepareResourceClaims is called by the kubelet for each batch of claims to
// prepare on this node.
func (d *Driver) PrepareResourceClaims(ctx context.Context, claims []*resourceapi.ResourceClaim) (map[types.UID]kubeletplugin.PrepareResult, error) {
	out := make(map[types.UID]kubeletplugin.PrepareResult, len(claims))
	for _, claim := range claims {
		out[claim.UID] = d.prepareOne(ctx, claim)
	}
	return out, nil
}

func (d *Driver) prepareOne(ctx context.Context, claim *resourceapi.ResourceClaim) kubeletplugin.PrepareResult {
	if claim.Status.Allocation == nil {
		return kubeletplugin.PrepareResult{Err: fmt.Errorf("claim %s not allocated", claim.UID)}
	}

	// Pods that will consume this claim (usually one).
	var podUIDs []string
	for _, ref := range claim.Status.ReservedFor {
		if ref.Resource == "pods" {
			podUIDs = append(podUIDs, string(ref.UID))
		}
	}

	var devices []kubeletplugin.Device
	var streams []checkpoint.StreamConfig
	for i := range claim.Status.Allocation.Devices.Results {
		res := claim.Status.Allocation.Devices.Results[i]
		if res.Driver != d.DriverName {
			continue // device for another driver
		}
		stream, err := d.GetStream(ctx, res.Device)
		if err != nil {
			return kubeletplugin.PrepareResult{Err: fmt.Errorf("resolve device %s: %w", res.Device, err)}
		}
		streams = append(streams, StreamConfigOf(stream))
		devices = append(devices, kubeletplugin.Device{
			Requests:   []string{res.Request},
			PoolName:   res.Pool,
			DeviceName: res.Device,
			// No CDIDeviceIDs: env injection is done by NRI AddEnv, not CDI.
		})
	}

	// Record pod->stream so NRI can program the data plane at pod start.
	for _, uid := range podUIDs {
		rec := checkpoint.PodRecord{
			PodUID:    uid,
			Namespace: claim.Namespace,
			Streams:   streams,
		}
		if err := d.Store.PutPod(rec); err != nil {
			return kubeletplugin.PrepareResult{Err: fmt.Errorf("checkpoint pod %s: %w", uid, err)}
		}
	}
	if err := d.Store.PutClaimPods(string(claim.UID), podUIDs); err != nil {
		klog.Errorf("checkpoint claim index %s: %v", claim.UID, err)
	}

	klog.Infof("prepared claim %s/%s: %d device(s), pods=%v", claim.Namespace, claim.Name, len(devices), podUIDs)
	return kubeletplugin.PrepareResult{Devices: devices}
}

// UnprepareResourceClaims undoes Prepare: removes the pod checkpoint records.
func (d *Driver) UnprepareResourceClaims(_ context.Context, claims []kubeletplugin.NamespacedObject) (map[types.UID]error, error) {
	out := make(map[types.UID]error, len(claims))
	for _, c := range claims {
		out[c.UID] = d.unprepareOne(c)
	}
	return out, nil
}

func (d *Driver) unprepareOne(c kubeletplugin.NamespacedObject) error {
	pods, err := d.Store.TakeClaimPods(string(c.UID))
	if err != nil {
		return err
	}
	// v1 assumes one claim per pod; deleting the pod record on unprepare is safe.
	for _, uid := range pods {
		if err := d.Store.DeletePod(uid); err != nil {
			klog.Errorf("delete checkpoint pod %s: %v", uid, err)
		}
	}
	klog.Infof("unprepared claim %s: removed %d pod record(s)", c.UID, len(pods))
	return nil
}

// HandleError logs background errors from the kubelet plugin helper. Recoverable
// errors are logged and retried by the helper; we do not crash the process.
func (d *Driver) HandleError(ctx context.Context, err error, msg string) {
	klog.FromContext(ctx).Error(err, "kubelet plugin background error", "msg", msg)
}

// StreamConfigOf projects a TSNStream CR into the checkpoint StreamConfig the NRI
// hooks consume, written to the checkpoint at NodePrepare (pod bind time).
func StreamConfigOf(s *v1alpha1.TSNStream) checkpoint.StreamConfig {
	cfg := checkpoint.StreamConfig{
		StreamID:  s.Spec.StreamID,
		Direction: string(s.Spec.Direction),
		IfName:    s.Spec.IfName,
		VLANID:    uint16(s.Spec.VLANID),
		PCP:       uint8(s.Spec.PCP),
		DstIP:     s.Spec.Dst.IP,
		DstPort:   uint16(s.Spec.Dst.Port),
		DstMAC:    s.Spec.Dst.MAC,
	}
	if s.Spec.Talker != nil {
		cfg.TalkerIP = s.Spec.Talker.IP
		cfg.TalkerMAC = s.Spec.Talker.MAC
	}
	return cfg
}
