package nri

import (
	"fmt"
	"net"
	"runtime"

	"github.com/vishvananda/netlink"
	"github.com/vishvananda/netns"

	"tsn.dra.net/dratsn/pkg/ebpf"
)

// attachDummyXDPInPod enters the pod netns at nsPath and attaches the dummy XDP
// program to the pod-side interface (ifInPod, default eth0). This is the receive
// side of the RX redirect: the NIC redirects out the host-side veth, and veth's
// ndo_xdp_xmit only delivers when the pod-side has XDP/NAPI up. The link is pinned
// under the pod UID so DetachVethXDP can remove it from the host netns later.
func attachDummyXDPInPod(nsPath, ifInPod string, loader *ebpf.Loader, podUID string) error {
	if ifInPod == "" {
		ifInPod = "eth0"
	}
	return inNetns(nsPath, func() error {
		link, err := netlink.LinkByName(ifInPod)
		if err != nil {
			return fmt.Errorf("lookup %s in netns: %w", ifInPod, err)
		}
		return loader.AttachVethXDP(link.Attrs().Index, podUID)
	})
}

// podNetInfo enters the pod network namespace at nsPath and returns the pod's
// primary IPv4 (on eth0), the pod's eth0 MAC, and the host-side veth ifindex
// (eth0's iflink / peer). The host veth ifindex is the XDP redirect target for
// the RX path; the MAC is rewritten into the frame so the pod stack accepts it.
func podNetInfo(nsPath, ifInPod string) (podIP, podMAC string, hostVethIfindex int, err error) {
	if ifInPod == "" {
		ifInPod = "eth0"
	}
	err = inNetns(nsPath, func() error {
		link, lerr := netlink.LinkByName(ifInPod)
		if lerr != nil {
			return fmt.Errorf("lookup %s in netns: %w", ifInPod, lerr)
		}
		// For a veth, Attrs().ParentIndex (IFLA_LINK) is the peer's ifindex,
		// which lives in the host namespace = the XDP redirect target.
		hostVethIfindex = link.Attrs().ParentIndex
		if hostVethIfindex == 0 {
			return fmt.Errorf("could not determine host veth ifindex for %s", ifInPod)
		}
		if hw := link.Attrs().HardwareAddr; len(hw) == 6 {
			podMAC = hw.String()
		}
		addrs, aerr := netlink.AddrList(link, netlink.FAMILY_V4)
		if aerr != nil {
			return fmt.Errorf("list addrs: %w", aerr)
		}
		for _, a := range addrs {
			if a.IP.To4() != nil && a.IP.IsGlobalUnicast() {
				podIP = a.IP.String()
				break
			}
		}
		if podIP == "" {
			return fmt.Errorf("no IPv4 on %s", ifInPod)
		}
		return nil
	})
	return podIP, podMAC, hostVethIfindex, err
}

// inNetns runs fn inside the network namespace at nsPath, restoring the original
// namespace afterwards. The OS thread is locked for the duration.
func inNetns(nsPath string, fn func() error) error {
	runtime.LockOSThread()
	defer runtime.UnlockOSThread()

	orig, err := netns.Get()
	if err != nil {
		return fmt.Errorf("get current netns: %w", err)
	}
	defer orig.Close()

	target, err := netns.GetFromPath(nsPath)
	if err != nil {
		return fmt.Errorf("open netns %s: %w", nsPath, err)
	}
	defer target.Close()

	if err := netns.Set(target); err != nil {
		return fmt.Errorf("enter netns %s: %w", nsPath, err)
	}
	defer netns.Set(orig) //nolint:errcheck

	return fn()
}

// nicInfo resolves a host NIC's MAC, primary IPv4, and ifindex by interface name.
// Used by the TX path to build the L2 frame (src=NIC MAC), the bpf_redirect target
// (ifindex), and the per-node SNAT source (the NIC's own IP). The NIC lives in the
// host netns (the DaemonSet is hostNetwork), so no netns entry needed.
func nicInfo(ifName string) (mac, ip string, ifindex int, err error) {
	iface, err := net.InterfaceByName(ifName)
	if err != nil {
		return "", "", 0, fmt.Errorf("lookup NIC %s: %w", ifName, err)
	}
	if len(iface.HardwareAddr) != 6 {
		return "", "", 0, fmt.Errorf("NIC %s has no 6-byte MAC", ifName)
	}
	return iface.HardwareAddr.String(), firstGlobalIPv4(iface), iface.Index, nil
}

// firstGlobalIPv4 returns the first non-loopback global-unicast IPv4 on iface, or "".
func firstGlobalIPv4(iface *net.Interface) string {
	addrs, err := iface.Addrs()
	if err != nil {
		return ""
	}
	for _, a := range addrs {
		var ip net.IP
		switch v := a.(type) {
		case *net.IPNet:
			ip = v.IP
		case *net.IPAddr:
			ip = v.IP
		}
		if ip4 := ip.To4(); ip4 != nil && ip4.IsGlobalUnicast() {
			return ip4.String()
		}
	}
	return ""
}

// firstIPv4 returns the first global-unicast IPv4 string from a list, or "".
func firstIPv4(ips []string) string {
	for _, s := range ips {
		ip := net.ParseIP(s)
		if ip != nil && ip.To4() != nil && ip.IsGlobalUnicast() {
			return ip.To4().String()
		}
	}
	return ""
}
