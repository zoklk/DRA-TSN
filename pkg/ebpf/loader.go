package ebpf

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"

	"github.com/cilium/ebpf"
	"github.com/cilium/ebpf/link"
	"k8s.io/klog/v2"
)

// DefaultPinDir is the bpffs directory holding the DRATSN maps and links.
const DefaultPinDir = "/sys/fs/bpf/tsn"

// Loader owns the loaded TX/RX/veth objects and the pinned maps. The RX (XDP)
// program attaches to physical TSN NICs; the TX (TCX ingress) program attaches
// per pod to the host-side veth, and the veth dummy XDP attaches per pod inside
// the pod netns (both driven by NRI).
type Loader struct {
	pinDir string
	tx     txObjects
	rx     rxObjects
	veth   vethObjects
}

// New loads the data-plane objects, pinning the maps under pinDir so they are
// shared across the TX/RX programs and reachable by the NRI hooks.
func New(pinDir string) (*Loader, error) {
	if pinDir == "" {
		pinDir = DefaultPinDir
	}
	if err := os.MkdirAll(pinDir, 0o755); err != nil {
		return nil, fmt.Errorf("mkdir pin dir %s: %w", pinDir, err)
	}
	l := &Loader{pinDir: pinDir}

	mapOpts := ebpf.CollectionOptions{Maps: ebpf.MapOptions{PinPath: pinDir}}
	if err := loadTxObjects(&l.tx, &mapOpts); err != nil {
		return nil, fmt.Errorf("load tx objects: %w", err)
	}
	if err := loadRxObjects(&l.rx, &mapOpts); err != nil {
		return nil, fmt.Errorf("load rx objects: %w", err)
	}
	if err := loadVethObjects(&l.veth, nil); err != nil {
		return nil, fmt.Errorf("load veth objects: %w", err)
	}
	klog.Infof("eBPF objects loaded; maps pinned under %s", pinDir)
	return l, nil
}

func (l *Loader) linksDir() string { return filepath.Join(l.pinDir, "links") }

// AttachNIC attaches, on ifName: the TX egress guard at the TCX HEAD of egress
// (lets veth-redirected TSN frames bypass cilium's VLAN filter) and the RX program
// at XDP ingress (native driver mode). Links are pinned so attachment survives the
// process; re-attaching is idempotent. (The main TX program attaches per pod to the
// host veth instead — see AttachVethTCX.)
func (l *Loader) AttachNIC(ifName string) error {
	iface, err := netInterfaceByName(ifName)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(l.linksDir(), 0o755); err != nil {
		return err
	}

	// TX egress guard: TCX HEAD on the NIC's egress. Lets TSN frames the veth
	// program redirected here (carrying TSN_TX_MARK) bypass cilium's cil_to_netdev
	// VLAN filter (TCX_PASS); all other egress falls through to cilium (TCX_NEXT).
	guardPin := filepath.Join(l.linksDir(), ifName+"-txguard")
	if !pinExists(guardPin) {
		gl, err := link.AttachTCX(link.TCXOptions{
			Interface: iface.Index,
			Program:   l.tx.TsnTxGuard,
			Attach:    ebpf.AttachTCXEgress,
			Anchor:    link.Head(),
		})
		if err != nil {
			return fmt.Errorf("attach TCX egress guard on %s: %w", ifName, err)
		}
		if err := gl.Pin(guardPin); err != nil {
			gl.Close()
			return fmt.Errorf("pin tx guard link: %w", err)
		}
		klog.Infof("attached TX egress guard (TCX HEAD) on %s", ifName)
	}

	// RX: XDP ingress, native driver mode.
	rxPin := filepath.Join(l.linksDir(), ifName+"-rx")
	if !pinExists(rxPin) {
		xl, err := link.AttachXDP(link.XDPOptions{
			Interface: iface.Index,
			Program:   l.rx.TsnRx,
			Flags:     link.XDPDriverMode,
		})
		if err != nil {
			// Fall back to generic (SKB) XDP if the NIC lacks native support.
			klog.V(2).Infof("native XDP on %s failed (%v), trying generic mode", ifName, err)
			xl, err = link.AttachXDP(link.XDPOptions{
				Interface: iface.Index,
				Program:   l.rx.TsnRx,
				Flags:     link.XDPGenericMode,
			})
		}
		if err != nil {
			return fmt.Errorf("attach XDP on %s: %w", ifName, err)
		}
		if err := xl.Pin(rxPin); err != nil {
			xl.Close()
			return fmt.Errorf("pin rx link: %w", err)
		}
		klog.Infof("attached RX (XDP native) on %s", ifName)
	}
	return nil
}

// DetachNIC removes the TX egress guard and RX XDP links pinned for ifName (the
// inverse of AttachNIC). Used when a NIC is dropped from the capable-nics label.
// Missing pins are not an error.
func (l *Loader) DetachNIC(ifName string) error {
	var firstErr error
	for _, pin := range []string{
		filepath.Join(l.linksDir(), ifName+"-txguard"),
		filepath.Join(l.linksDir(), ifName+"-rx"),
	} {
		lk, err := link.LoadPinnedLink(pin, nil)
		if err != nil {
			if errors.Is(err, os.ErrNotExist) {
				continue
			}
			if firstErr == nil {
				firstErr = err
			}
			continue
		}
		_ = lk.Unpin()
		if err := lk.Close(); err != nil && firstErr == nil {
			firstErr = err
		}
	}
	if firstErr == nil {
		klog.Infof("detached TSN links on %s", ifName)
	}
	return firstErr
}

// AttachVethTCX attaches the TX program at the TCX HEAD of the host-side veth's
// ingress (= the pod's egress). Matching flows are tagged + redirected to the NIC;
// non-matching return TCX_NEXT so cilium's from-container handles them. The link
// is pinned under the pod UID so DetachVethTCX can find it at pod stop.
// The host veth lives in the host netns (the DaemonSet is hostNetwork), so no
// netns entry is needed. Re-attaching is idempotent.
func (l *Loader) AttachVethTCX(hostVethIfindex int, podUID string) error {
	if err := os.MkdirAll(l.tcxDir(), 0o755); err != nil {
		return err
	}
	pin := l.tcxPin(podUID)
	if pinExists(pin) {
		return nil
	}
	tl, err := link.AttachTCX(link.TCXOptions{
		Interface: hostVethIfindex,
		Program:   l.tx.TsnTx,
		Attach:    ebpf.AttachTCXIngress,
		Anchor:    link.Head(),
	})
	if err != nil {
		return fmt.Errorf("attach TCX ingress on host veth %d: %w", hostVethIfindex, err)
	}
	if err := tl.Pin(pin); err != nil {
		tl.Close()
		return fmt.Errorf("pin tx veth link: %w", err)
	}
	klog.V(2).Infof("attached TX (TCX HEAD ingress) on host veth %d", hostVethIfindex)
	return nil
}

// DetachVethTCX removes the TX TCX link pinned for a pod.
func (l *Loader) DetachVethTCX(podUID string) error {
	pin := l.tcxPin(podUID)
	lk, err := link.LoadPinnedLink(pin, nil)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil
		}
		return err
	}
	_ = lk.Unpin()
	return lk.Close()
}

// AttachVethXDP attaches the dummy XDP program to the pod-side interface so that
// the NIC's XDP redirect (which transmits out the host-side veth, the devmap
// target) can be accepted by veth_ndo_xdp_xmit: that helper delivers to the peer
// and requires the *receiving* side (the pod's eth0) to have XDP/NAPI enabled.
// Attaching to the host-side veth instead yields redirect err=-95 (EOPNOTSUPP).
//
// attachIfindex is the interface index *in the current network namespace* — the
// caller is responsible for having entered the pod netns (see nri.attachDummyXDPInPod).
// podUID keys the pin path so DetachVethXDP can find it from the host netns at
// pod stop (see vethPin for why the key is not the ifindex).
func (l *Loader) AttachVethXDP(attachIfindex int, podUID string) error {
	if err := os.MkdirAll(l.vethDir(), 0o755); err != nil {
		return err
	}
	pin := l.vethPin(podUID)
	if pinExists(pin) {
		return nil
	}
	xl, err := link.AttachXDP(link.XDPOptions{
		Interface: attachIfindex,
		Program:   l.veth.TsnVethDummy,
		Flags:     link.XDPDriverMode,
	})
	if err != nil {
		// Fall back to generic (SKB) mode: veth commonly rejects driver mode.
		xl, err = link.AttachXDP(link.XDPOptions{
			Interface: attachIfindex,
			Program:   l.veth.TsnVethDummy,
			Flags:     link.XDPGenericMode,
		})
		if err != nil {
			return fmt.Errorf("attach veth XDP on ifindex %d: %w", attachIfindex, err)
		}
	}
	if err := xl.Pin(pin); err != nil {
		xl.Close()
		return fmt.Errorf("pin veth link: %w", err)
	}
	klog.V(2).Infof("attached dummy XDP on pod ifindex %d (pinned for pod %s)", attachIfindex, podUID)
	return nil
}

// DetachVethXDP removes the dummy XDP link pinned for a pod.
func (l *Loader) DetachVethXDP(podUID string) error {
	pin := l.vethPin(podUID)
	lk, err := link.LoadPinnedLink(pin, nil)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil
		}
		return err
	}
	_ = lk.Unpin()
	return lk.Close()
}

func (l *Loader) vethDir() string { return filepath.Join(l.pinDir, "veth") }

// Pins are keyed by pod UID, not by veth ifindex. The kernel reuses ifindexes,
// so an ifindex-keyed pin left behind by a pod that was deleted while this
// driver was down would make the next pod's attach a silent no-op (pinExists
// short-circuits) while the logs still report success.
func (l *Loader) vethPin(podUID string) string { return filepath.Join(l.vethDir(), podUID) }
func (l *Loader) tcxDir() string               { return filepath.Join(l.pinDir, "tcx") }
func (l *Loader) tcxPin(podUID string) string  { return filepath.Join(l.tcxDir(), podUID) }

// Map accessors for the NRI hooks.
func (l *Loader) TxFlows() *ebpf.Map   { return l.tx.TxFlows }
func (l *Loader) RxFlows() *ebpf.Map   { return l.rx.RxFlows }
func (l *Loader) XdpDevmap() *ebpf.Map { return l.rx.XdpDevmap }

// Latency-measurement timeline maps (ADR-0028), pinned under pinDir as tx_ts/rx_ts.
// Read from a shell with `bpftool map dump pinned /sys/fs/bpf/tsn/tx_ts`. Stale
// entries from a prior run are filtered by the collector via absolute TAI time
// (any timestamp < run start = leftover), so no per-run clear is needed.
func (l *Loader) TxTs() *ebpf.Map { return l.tx.TxTs }
func (l *Loader) RxTs() *ebpf.Map { return l.rx.RxTs }

// Close releases program/map handles (pinned objects persist in bpffs).
func (l *Loader) Close() {
	l.tx.Close()
	l.rx.Close()
	l.veth.Close()
}

func pinExists(p string) bool {
	_, err := os.Stat(p)
	return err == nil
}
