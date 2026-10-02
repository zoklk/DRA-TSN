// Package controller implements the TSN controller's Stream Mapper: it joins CNC
// streams with node NIC inventory (TSNNic) and emits TSNStream CRs. It is a
// read-only CNC observer; the only writes it makes are to TSNStream CRs.
package controller

import (
	"context"
	"fmt"
	"regexp"
	"strings"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/klog/v2"
	"sigs.k8s.io/controller-runtime/pkg/client"

	"tsn.dra.net/dratsn/api/v1alpha1"
	"tsn.dra.net/dratsn/pkg/cnc"
)

// ManagedByLabel marks TSNStream CRs owned by this controller so full-sync only
// reconciles its own output (single-direction pipeline: read TSNNic, write TSNStream).
const ManagedByLabel = "tsn.dra.net/managed-by"
const managedByValue = "tsn-controller"

// Mapper joins CNC streams x TSNNic inventory into TSNStream CRs.
type Mapper struct {
	Client client.Client
	// Reader is an uncached client (APIReader) used for the read-modify-write
	// reconcile so we never act on a stale informer cache.
	Reader     client.Reader
	CNC        cnc.Client
	ResyncTime time.Duration
}

// Start runs the reconcile loop until ctx is done. It implements
// manager.Runnable so it only runs on the elected leader.
func (m *Mapper) Start(ctx context.Context) error {
	if m.ResyncTime == 0 {
		m.ResyncTime = 30 * time.Second
	}
	klog.Info("Stream Mapper starting")

	if err := cnc.DialWait(ctx, m.CNC); err != nil {
		return err
	}

	// Subscribe to on-change; fall back to periodic resync if it drops.
	events, err := m.CNC.Subscribe(ctx)
	if err != nil {
		klog.Infof("CNC subscribe failed, relying on resync: %v", err)
		events = nil
	}

	ticker := time.NewTicker(m.ResyncTime)
	defer ticker.Stop()

	// Prime once on startup.
	m.reconcileOnce(ctx)

	// Re-subscription backoff. A nil events channel blocks forever in the
	// select, so a single failed re-subscribe used to downgrade the controller
	// to polling for the rest of its life; the retry is what makes "on-change
	// first, polling as fallback" true at runtime rather than only on startup.
	// The backoff also keeps a CNC that accepts and immediately closes the
	// stream from spinning this loop.
	backoff := resubscribeMin
	resubscribe := func() {
		var err error
		if events, err = m.CNC.Subscribe(ctx); err != nil {
			klog.Infof("CNC re-subscribe failed, retrying in %s: %v", backoff, err)
			events = nil
			return
		}
		klog.Info("CNC event stream re-subscribed")
		backoff = resubscribeMin
	}

	// one timer for the whole loop: a per-iteration timer would either leak
	// (deferred Stop only runs when Run returns) or need stopping on every
	// branch, including the ones that continue
	retryTimer := time.NewTimer(resubscribeMax)
	retryTimer.Stop()
	defer retryTimer.Stop()

	for {
		var retry <-chan time.Time
		if events == nil {
			retryTimer.Reset(backoff)
			retry = retryTimer.C
		}
		select {
		case <-ctx.Done():
			return nil
		case <-retry:
			if backoff < resubscribeMax {
				backoff = min(backoff*2, resubscribeMax)
			}
			resubscribe()
		case <-ticker.C:
			m.reconcileOnce(ctx)
		case ev, ok := <-events:
			if !ok {
				// Stream closed: re-subscribe and resync.
				klog.Info("CNC event stream closed; re-subscribing")
				resubscribe()
				m.reconcileOnce(ctx)
				continue
			}
			klog.V(2).Infof("CNC on-change: %s %s", ev.Type, ev.Stream.StreamID)
			m.reconcileOnce(ctx)
		}
	}
}

// Re-subscription backoff bounds. The ceiling stays under the resync period so
// a controller running on polling alone still retries on-change regularly.
const (
	resubscribeMin = 2 * time.Second
	resubscribeMax = 20 * time.Second
)

// reconcileOnce computes the desired TSNStream set and converges the cluster to
// it (create/update/delete). Errors are logged; the next tick retries
// (eventually-consistent).
func (m *Mapper) reconcileOnce(ctx context.Context) {
	if err := m.reconcile(ctx); err != nil {
		klog.Errorf("reconcile error: %v", err)
	}
}

func (m *Mapper) reconcile(ctx context.Context) error {
	streams, err := m.CNC.ListStreams(ctx)
	if err != nil {
		return fmt.Errorf("list CNC streams: %w", err)
	}

	var nicList v1alpha1.TSNNicList
	if err := m.Reader.List(ctx, &nicList); err != nil {
		return fmt.Errorf("list TSNNic: %w", err)
	}

	desired := DesiredStreams(streams, nicList.Items)

	var existing v1alpha1.TSNStreamList
	if err := m.Reader.List(ctx, &existing, client.MatchingLabels{ManagedByLabel: managedByValue}); err != nil {
		return fmt.Errorf("list TSNStream: %w", err)
	}
	existingByName := map[string]*v1alpha1.TSNStream{}
	for i := range existing.Items {
		existingByName[existing.Items[i].Name] = &existing.Items[i]
	}

	klog.V(2).Infof("reconcile: cncStreams=%d nics=%d desired=%d existing=%d", len(streams), len(nicList.Items), len(desired), len(existing.Items))

	// Create or update.
	for name, want := range desired {
		cur, ok := existingByName[name]
		if !ok {
			err := m.Client.Create(ctx, &want)
			switch {
			case err == nil:
				klog.Infof("created TSNStream %s (stream %s dir %s node %s/%s)", name, want.Spec.StreamID, want.Spec.Direction, want.Spec.NodeName, want.Spec.IfName)
			case apierrors.IsAlreadyExists(err):
				klog.V(2).Infof("TSNStream %s already exists; will reconcile spec on next pass", name)
			default:
				klog.Errorf("create TSNStream %s: %v", name, err)
			}
			continue
		}
		if !specEqual(cur.Spec, want.Spec) {
			cur.Spec = want.Spec
			if err := m.Client.Update(ctx, cur); err != nil {
				klog.Errorf("update TSNStream %s: %v", name, err)
			} else {
				klog.Infof("updated TSNStream %s", name)
			}
		}
	}

	// Delete stale (full-sync: stream removed from CNC or NIC gone).
	for name, cur := range existingByName {
		if _, ok := desired[name]; !ok {
			if err := m.Client.Delete(ctx, cur); err != nil && !apierrors.IsNotFound(err) {
				klog.Errorf("delete TSNStream %s: %v", name, err)
			} else {
				klog.Infof("deleted stale TSNStream %s (no longer in CNC/NIC join)", name)
			}
		}
	}
	return nil
}

// DesiredStreams computes the TSNStream CRs that should exist given the current
// CNC streams and node NIC inventory. For each stream, a NIC matching the talker
// endpoint yields a TX stream (dst = listener), and a NIC matching the listener
// endpoint yields an RX stream (dst = that node's NIC host endpoint).
func DesiredStreams(streams []cnc.Stream, nics []v1alpha1.TSNNic) map[string]v1alpha1.TSNStream {
	out := map[string]v1alpha1.TSNStream{}
	for _, s := range streams {
		for _, nic := range nics {
			if !nic.Spec.TSNCapable {
				continue
			}
			if endpointMatchesNIC(s.Talker, nic) {
				ts := buildStream(s, nic, v1alpha1.DirectionTX)
				out[ts.Name] = ts
			}
			if endpointMatchesNIC(s.Listener, nic) {
				ts := buildStream(s, nic, v1alpha1.DirectionRX)
				out[ts.Name] = ts
			}
		}
	}
	return out
}

func buildStream(s cnc.Stream, nic v1alpha1.TSNNic, dir v1alpha1.StreamDirection) v1alpha1.TSNStream {
	var dst v1alpha1.Dst
	var talker, listener *v1alpha1.Endpoint
	talker = &v1alpha1.Endpoint{IP: s.Talker.IP, MAC: s.Talker.MAC}
	listener = &v1alpha1.Endpoint{IP: s.Listener.IP, MAC: s.Listener.MAC}
	if dir == v1alpha1.DirectionTX {
		// vPLC -> robot: dst is the external listener.
		dst = v1alpha1.Dst{IP: s.Listener.IP, Port: s.Port, MAC: s.Listener.MAC}
	} else {
		// sensor -> vPLC: dst is this node's TSN NIC host endpoint (static receive point).
		dst = v1alpha1.Dst{IP: nic.Spec.HostIP, Port: s.Port, MAC: nic.Spec.MACAddress}
	}
	name := streamName(s.StreamID, nic.Spec.NodeName, nic.Spec.IfName, dir)
	return v1alpha1.TSNStream{
		ObjectMeta: metav1.ObjectMeta{
			Name:   name,
			Labels: map[string]string{ManagedByLabel: managedByValue},
		},
		Spec: v1alpha1.TSNStreamSpec{
			StreamID:  s.StreamID,
			NodeName:  nic.Spec.NodeName,
			IfName:    nic.Spec.IfName,
			Direction: dir,
			Dst:       dst,
			VLANID:    s.VLANID,
			PCP:       s.PCP,
			DomainID:  s.DomainID,
			Talker:    talker,
			Listener:  listener,

			MaxLatencyNs:         int64(s.MaxLatency),
			AccumulatedLatencyNs: int64(s.AccumulatedLatency),
		},
	}
}

func endpointMatchesNIC(ep cnc.Endpoint, nic v1alpha1.TSNNic) bool {
	if ep.MAC != "" && nic.Spec.MACAddress != "" && strings.EqualFold(ep.MAC, nic.Spec.MACAddress) {
		return true
	}
	if ep.IP != "" && nic.Spec.HostIP != "" && ep.IP == nic.Spec.HostIP {
		return true
	}
	return false
}

var nameSanitize = regexp.MustCompile(`[^a-z0-9-]`)

func streamName(streamID, node, ifName string, dir v1alpha1.StreamDirection) string {
	raw := fmt.Sprintf("%s-%s-%s-%s", streamID, node, ifName, strings.ToLower(string(dir)))
	raw = strings.ToLower(raw)
	raw = nameSanitize.ReplaceAllString(raw, "-")
	raw = strings.Trim(raw, "-")
	if len(raw) > 253 {
		raw = raw[:253]
	}
	return raw
}

func specEqual(a, b v1alpha1.TSNStreamSpec) bool {
	return a.StreamID == b.StreamID &&
		a.NodeName == b.NodeName &&
		a.IfName == b.IfName &&
		a.Direction == b.Direction &&
		a.Dst == b.Dst &&
		a.VLANID == b.VLANID &&
		a.PCP == b.PCP &&
		a.DomainID == b.DomainID &&
		a.MaxLatencyNs == b.MaxLatencyNs &&
		a.AccumulatedLatencyNs == b.AccumulatedLatencyNs &&
		endpointEqual(a.Talker, b.Talker) &&
		endpointEqual(a.Listener, b.Listener)
}

// endpointEqual is nil-safe value comparison (Endpoint is a comparable struct).
// Without Talker/Listener here, a CNC stream that changes only its talker/listener
// (same streamId) never updates the TSNStream — RX talker drives the rx_flows key,
// so a stale talker silently breaks DNAT (echo misses the pod).
func endpointEqual(a, b *v1alpha1.Endpoint) bool {
	if a == nil || b == nil {
		return a == b
	}
	return *a == *b
}
