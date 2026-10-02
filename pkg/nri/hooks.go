// Package nri wires pod lifecycle to the DRATSN data plane. At pod start it reads
// the Phase 2 checkpoint, programs the TX/RX bpf maps, attaches the dummy XDP to
// the pod veth, and injects the TSN_STREAMS env. At pod stop it tears all of that down.
// On restart Synchronize rebuilds map state from the checkpoint.
//
// All hooks are idempotent and fail-safe: a programming failure for a TSN stream
// never affects non-TSN traffic (map miss = cilium normal path).
package nri

import (
	"context"
	"encoding/json"
	"fmt"
	"sync"
	"time"

	"github.com/containerd/nri/pkg/api"
	"github.com/containerd/nri/pkg/stub"
	"k8s.io/klog/v2"

	"tsn.dra.net/dratsn/pkg/checkpoint"
	"tsn.dra.net/dratsn/pkg/ebpf"
)

const udpProto uint8 = 17

// Plugin implements the NRI hooks we care about.
type Plugin struct {
	// mu serializes the NRI lifecycle hooks (Run/Stop/Remove/Synchronize), which the
	// stub may dispatch concurrently and which all read-modify-write the shared bpf
	// maps + BoltDB checkpoint (GetPod/ListPods -> program/deprogram -> PutPod). These
	// are cold paths (pod start/stop), so a single coarse lock is cheap and correct.
	// Held only in the public entry points; internal helpers (program/programStream/
	// deprogram*) stay lock-free to avoid re-entrant deadlock.
	mu     sync.Mutex
	stub   stub.Stub
	store  *checkpoint.Store
	loader *ebpf.Loader
	// SNAT (TX): rewrite the pod source IP before tagging. snatToNICIP rewrites to
	// the *egress NIC's own IP* (correct per-node default — each node SNATs to its
	// local TSN NIC). snatOverrideIP, if set, forces a fixed IP instead (rare; e.g.
	// a switchless lab). Neither set = pass-through (src stays pod IP).
	snatToNICIP    bool
	snatOverrideIP string
}

// New builds the NRI plugin and its stub. Call Run to start serving.
func New(store *checkpoint.Store, loader *ebpf.Loader, driverName string, snatToNICIP bool, snatOverrideIP string) (*Plugin, error) {
	p := &Plugin{store: store, loader: loader, snatToNICIP: snatToNICIP, snatOverrideIP: snatOverrideIP}
	s, err := stub.New(p,
		stub.WithPluginName(driverName),
		stub.WithPluginIdx("10"),
		stub.WithOnClose(func() { klog.Infof("%s NRI plugin connection closed", driverName) }),
	)
	if err != nil {
		return nil, fmt.Errorf("create NRI stub: %w", err)
	}
	p.stub = s
	return p, nil
}

// Run serves the NRI plugin until ctx is done, retrying on connection loss.
func (p *Plugin) Run(ctx context.Context) {
	const maxAttempts = 10
	for i := 0; i < maxAttempts; i++ {
		if err := p.stub.Run(ctx); err != nil {
			klog.Infof("NRI plugin run error: %v", err)
		}
		select {
		case <-ctx.Done():
			return
		default:
		}
		time.Sleep(time.Second)
	}
	klog.Errorf("NRI plugin gave up after %d attempts", maxAttempts)
}

// Synchronize rebuilds data-plane state from the checkpoint after a restart and
// prunes entries for pods that no longer exist.
func (p *Plugin) Synchronize(_ context.Context, pods []*api.PodSandbox, _ []*api.Container) ([]*api.ContainerUpdate, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	live := map[string]*api.PodSandbox{}
	for _, pod := range pods {
		live[pod.GetUid()] = pod
	}
	recs, err := p.store.ListPods()
	if err != nil {
		klog.Errorf("Synchronize: list checkpoint: %v", err)
		return nil, nil
	}
	for i := range recs {
		rec := recs[i]
		if pod, ok := live[rec.PodUID]; ok {
			if err := p.program(pod, &rec); err != nil {
				klog.Errorf("Synchronize: reprogram pod %s: %v", rec.PodUID, err)
			}
		} else {
			klog.Infof("Synchronize: pruning data plane for gone pod %s", rec.PodUID)
			p.deprogram(&rec)
			_ = p.store.DeletePod(rec.PodUID)
		}
	}
	klog.Infof("NRI Synchronize: %d live pods, %d checkpoint records", len(pods), len(recs))
	return nil, nil
}

// RunPodSandbox programs the data plane for a pod that has a TSN claim.
func (p *Plugin) RunPodSandbox(_ context.Context, pod *api.PodSandbox) error {
	p.mu.Lock()
	defer p.mu.Unlock()
	rec, ok, err := p.store.GetPod(pod.GetUid())
	if err != nil || !ok {
		return nil // not our pod (map miss = normal traffic)
	}
	if err := p.program(pod, &rec); err != nil {
		klog.Errorf("RunPodSandbox: program pod %s/%s: %v", pod.GetNamespace(), pod.GetName(), err)
		// non-fatal: do not block pod start; TSN stream simply won't be tagged
	}
	return nil
}

// streamEnv is one entry in the TSN_STREAMS env JSON. A vPLC controlling a cell
// may bind many streams (sensors RX + actuators TX), so env exposes all of them
// as a JSON array rather than a single flat set. The app sends/receives normal
// UDP; VLAN/PCP tagging is done transparently by eBPF and is not surfaced here.
// TX entries carry the external dst (ip:port); RX entries carry the local port
// the app should listen on (the node endpoint is DNATed to it by eBPF).
type streamEnv struct {
	ID   string `json:"id"`
	Dir  string `json:"dir"`
	Dst  string `json:"dst,omitempty"`  // TX: "ip:port"
	Port int    `json:"port,omitempty"` // RX: local listen port
}

// CreateContainer injects TSN_STREAMS (JSON array of all bound streams) so the
// app can target/listen per stream regardless of how many it claims.
func (p *Plugin) CreateContainer(_ context.Context, pod *api.PodSandbox, _ *api.Container) (*api.ContainerAdjustment, []*api.ContainerUpdate, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	rec, ok, err := p.store.GetPod(pod.GetUid())
	if err != nil || !ok || len(rec.Streams) == 0 {
		return nil, nil, nil
	}
	entries := make([]streamEnv, 0, len(rec.Streams))
	for _, cfg := range rec.Streams {
		e := streamEnv{ID: cfg.StreamID, Dir: cfg.Direction}
		if cfg.Direction == "RX" {
			e.Port = int(cfg.DstPort)
		} else {
			e.Dst = fmt.Sprintf("%s:%d", cfg.DstIP, cfg.DstPort)
		}
		entries = append(entries, e)
	}
	buf, err := json.Marshal(entries)
	if err != nil {
		klog.Errorf("marshal TSN_STREAMS for pod %s: %v", pod.GetUid(), err)
		return nil, nil, nil
	}
	adj := &api.ContainerAdjustment{}
	adj.AddEnv("TSN_STREAMS", string(buf))
	klog.V(2).Infof("injected TSN_STREAMS (%d stream(s)) into pod %s/%s", len(entries), pod.GetNamespace(), pod.GetName())
	return adj, nil, nil
}

// StopPodSandbox tears down the data plane for the pod.
func (p *Plugin) StopPodSandbox(_ context.Context, pod *api.PodSandbox) error {
	p.mu.Lock()
	defer p.mu.Unlock()
	rec, ok, err := p.store.GetPod(pod.GetUid())
	if err != nil || !ok {
		return nil
	}
	p.deprogram(&rec)
	return nil
}

// RemovePodSandbox removes the checkpoint record (final cleanup).
func (p *Plugin) RemovePodSandbox(_ context.Context, pod *api.PodSandbox) error {
	p.mu.Lock()
	defer p.mu.Unlock()
	rec, ok, _ := p.store.GetPod(pod.GetUid())
	if ok {
		p.deprogram(&rec)
	}
	_ = p.store.DeletePod(pod.GetUid())
	return nil
}

// program resolves the pod's runtime net info, programs every stream, and
// persists the runtime fields (PodIP/PodMAC/HostVethIfindex) so teardown and live
// TSNStream updates can rebuild map keys without re-entering the netns.
func (p *Plugin) program(pod *api.PodSandbox, rec *checkpoint.PodRecord) error {
	ns := networkNamespace(pod)
	if ns == "" {
		return fmt.Errorf("pod %s uses host network; cannot program TSN data plane", pod.GetUid())
	}

	podIP, podMAC, hostVeth, err := podNetInfo(ns, "eth0")
	if err != nil {
		// Fall back to the NRI-reported pod IP (TX needs only the pod IP).
		podIP = firstIPv4(pod.GetIps())
		if podIP == "" {
			return fmt.Errorf("resolve pod net info: %w", err)
		}
		klog.Warningf("pod %s: using NRI-reported IP %s, RX path unavailable (%v)", pod.GetUid(), podIP, err)
	}
	rec.PodIP = podIP
	// Only overwrite what was actually resolved. On the fallback path podMAC is
	// "" and hostVeth is 0, and persisting those would erase a previously good
	// record: the teardown in StopPodSandbox is guarded on exactly these two
	// fields, so a transient netns failure during Synchronize would leak the
	// pod's map entries and bpf links for the rest of the node's life.
	if podMAC != "" {
		rec.PodMAC = podMAC
	}
	if hostVeth != 0 {
		rec.HostVethIfindex = hostVeth
	}

	for i := range rec.Streams {
		p.programStream(rec, &rec.Streams[i], ns)
	}

	// Persist runtime fields so teardown / live update can rebuild keys without netns.
	if err := p.store.PutPod(*rec); err != nil {
		klog.Errorf("checkpoint update pod %s: %v", rec.PodUID, err)
	}
	return nil
}

// programStream inserts the bpf map entry for a single stream using the runtime
// fields already on rec (PodIP/PodMAC/HostVethIfindex). nsPath is the pod netns
// path for the one step that needs it (RX dummy XDP attach inside the pod).
func (p *Plugin) programStream(rec *checkpoint.PodRecord, cfg *checkpoint.StreamConfig, nsPath string) {
	switch cfg.Direction {
	case "TX":
		// TX runs at the host-side veth ingress and redirects to the NIC, so it
		// needs the host veth ifindex; the NIC isn't attached for TX.
		if rec.HostVethIfindex == 0 {
			klog.Warningf("TX stream %s: no host veth ifindex; skipping (TX attaches to the veth)", cfg.StreamID)
			return
		}
		// TSN is an L2 bridge: the frame must carry the listener's MAC as dst.
		// Without it from the CNC we cannot build a valid frame -> skip (map
		// miss = cilium normal path) rather than emit a broken L2 frame.
		if cfg.DstMAC == "" {
			klog.Warningf("TX stream %s: no listener MAC from CNC; skipping TX (map miss = cilium normal)", cfg.StreamID)
			return
		}
		nicMAC, nicIP, nicIfindex, nerr := nicInfo(cfg.IfName)
		if nerr != nil {
			klog.Errorf("TX stream %s: resolve NIC %s: %v", cfg.StreamID, cfg.IfName, nerr)
			return
		}
		if err := p.loader.AttachVethTCX(rec.HostVethIfindex, rec.PodUID); err != nil {
			klog.Errorf("attach veth TCX (host veth %d): %v", rec.HostVethIfindex, err)
			return
		}
		// SNAT source: explicit override > egress NIC's own IP > pass-through.
		snatIP := ""
		switch {
		case p.snatOverrideIP != "":
			snatIP = p.snatOverrideIP
		case p.snatToNICIP:
			if nicIP == "" {
				klog.Warningf("TX stream %s: SNAT requested but NIC %s has no IPv4; pass-through", cfg.StreamID, cfg.IfName)
			}
			snatIP = nicIP // "" -> NewTXVal leaves SNAT off
		}
		key, kerr := ebpf.NewTXKey(rec.PodIP, cfg.DstIP, cfg.DstPort, udpProto)
		if kerr != nil {
			klog.Errorf("tx key: %v", kerr)
			return
		}
		val, verr := ebpf.NewTXVal(cfg.VLANID, cfg.PCP, snatIP, cfg.DstMAC, nicMAC, uint32(nicIfindex))
		if verr != nil {
			klog.Errorf("tx val: %v", verr)
			return
		}
		if err := p.loader.PutTX(key, val); err != nil {
			klog.Errorf("put tx_flows: %v", err)
			return
		}
		klog.Infof("programmed TX stream %s pod %s -> %s:%d (dmac %s via NIC %s/%d) vlan %d pcp %d snat=%q", cfg.StreamID, rec.PodIP, cfg.DstIP, cfg.DstPort, cfg.DstMAC, cfg.IfName, nicIfindex, cfg.VLANID, cfg.PCP, snatIP)
	case "RX":
		if err := p.loader.AttachNIC(cfg.IfName); err != nil {
			klog.Errorf("attach NIC %s: %v", cfg.IfName, err)
			return
		}
		if rec.HostVethIfindex == 0 {
			klog.Warningf("RX stream %s: no host veth ifindex, skipping redirect", cfg.StreamID)
			return
		}
		// Attach the dummy XDP to the pod-side eth0 (not the host veth): the
		// redirect transmits out the host veth and veth_ndo_xdp_xmit only delivers
		// if the receiving (pod) side has XDP/NAPI up. Only needed at pod start;
		// a live update (nsPath=="") leaves the existing attach in place.
		if nsPath != "" {
			if err := attachDummyXDPInPod(nsPath, "eth0", p.loader, rec.PodUID); err != nil {
				klog.Errorf("attach pod veth XDP: %v", err)
				return
			}
		}
		// Talker IP is part of the rx_flows key (ADR-0027 stream-id hardening) — a
		// hash lookup can't wildcard it. Without it the entry would never match, so
		// skip programming (map miss = cilium normal path) instead of inserting a
		// dead entry.
		if cfg.TalkerIP == "" {
			klog.Warningf("RX stream %s: no talker IP from CNC; skipping RX (map miss = cilium normal)", cfg.StreamID)
			return
		}
		key, kerr := ebpf.NewRXKey(cfg.TalkerIP, cfg.DstIP, cfg.DstPort, udpProto) // cfg.DstIP = node endpoint
		if kerr != nil {
			klog.Errorf("rx key: %v", kerr)
			return
		}
		if rec.PodMAC == "" {
			klog.Warningf("RX stream %s: no pod MAC resolved; frames may be dropped on L2 mismatch", cfg.StreamID)
		}
		val, verr := ebpf.NewRXVal(rec.PodIP, cfg.DstPort, rec.PodMAC, uint32(rec.HostVethIfindex), cfg.VLANID, cfg.TalkerMAC)
		if verr != nil {
			klog.Errorf("rx val: %v", verr)
			return
		}
		if err := p.loader.PutRX(key, val); err != nil {
			klog.Errorf("put rx_flows: %v", err)
			return
		}
		klog.Infof("programmed RX stream %s talker %s -> node %s:%d -> pod %s mac %s (veth %d) vid %d tmac %q", cfg.StreamID, cfg.TalkerIP, cfg.DstIP, cfg.DstPort, rec.PodIP, rec.PodMAC, rec.HostVethIfindex, cfg.VLANID, cfg.TalkerMAC)
	}
}

// deprogramStream removes the bpf map entry for a single stream (no veth detach).
func (p *Plugin) deprogramStream(rec *checkpoint.PodRecord, cfg *checkpoint.StreamConfig) {
	switch cfg.Direction {
	case "TX":
		if rec.PodIP == "" {
			return
		}
		if key, err := ebpf.NewTXKey(rec.PodIP, cfg.DstIP, cfg.DstPort, udpProto); err == nil {
			_ = p.loader.DelTX(key)
		}
	case "RX":
		if cfg.TalkerIP == "" {
			return
		}
		if key, err := ebpf.NewRXKey(cfg.TalkerIP, cfg.DstIP, cfg.DstPort, udpProto); err == nil {
			_ = p.loader.DelRX(key)
		}
	}
}

// deprogram removes all bpf entries / veth attachments for a pod.
func (p *Plugin) deprogram(rec *checkpoint.PodRecord) {
	for i := range rec.Streams {
		p.deprogramStream(rec, &rec.Streams[i])
	}
	if rec.HostVethIfindex != 0 {
		_ = p.loader.DelDevmap(uint32(rec.HostVethIfindex))
		_ = p.loader.DetachVethXDP(rec.PodUID)
		_ = p.loader.DetachVethTCX(rec.PodUID)
	}
}

// networkNamespace returns the pod's network namespace path, or "" for host net.
func networkNamespace(pod *api.PodSandbox) string {
	for _, ns := range pod.GetLinux().GetNamespaces() {
		if ns.Type == "network" {
			return ns.Path
		}
	}
	return ""
}
