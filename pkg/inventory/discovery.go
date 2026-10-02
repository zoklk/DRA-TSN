// Package inventory discovers TSN-capable NICs on the local node and publishes
// them as TSNNic CRs for the TSN controller to join. v1 uses an explicit node
// label (tsn.dra.net/capable-nics) rather than auto-probing ethtool features.
package inventory

import (
	"net"
	"os"
	"path/filepath"
	"strings"

	"k8s.io/klog/v2"

	"tsn.dra.net/dratsn/api/v1alpha1"
)

// CapableNicsLabel is the node label listing TSN-capable NICs (comma-separated
// interface names), e.g. tsn.dra.net/capable-nics=eno5,eno6.
const CapableNicsLabel = "tsn.dra.net/capable-nics"

// NIC is a discovered TSN-capable interface.
type NIC struct {
	IfName     string
	MACAddress string
	PCIAddress string
	HostIP     string
}

// Discover inspects the named interfaces and returns their inventory. Interfaces
// that don't exist are skipped with a warning (label may reference NICs not on
// every node).
func Discover(ifNames []string) []NIC {
	var out []NIC
	for _, name := range ifNames {
		name = strings.TrimSpace(name)
		if name == "" {
			continue
		}
		iface, err := net.InterfaceByName(name)
		if err != nil {
			klog.Warningf("NIC discovery: interface %q not found: %v", name, err)
			continue
		}
		nic := NIC{
			IfName:     name,
			MACAddress: iface.HardwareAddr.String(),
			PCIAddress: readPCIAddress(name),
			HostIP:     firstGlobalIPv4(iface),
		}
		out = append(out, nic)
	}
	return out
}

// readPCIAddress resolves /sys/class/net/<if>/device symlink to a PCI address.
func readPCIAddress(ifName string) string {
	dev, err := os.Readlink(filepath.Join("/sys/class/net", ifName, "device"))
	if err != nil {
		return ""
	}
	return filepath.Base(dev)
}

// firstGlobalIPv4 returns the first non-loopback global-unicast IPv4 on iface.
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
		if ip == nil {
			continue
		}
		ip4 := ip.To4()
		if ip4 == nil || !ip4.IsGlobalUnicast() {
			continue
		}
		return ip4.String()
	}
	return ""
}

// nicSpecEqual reports whether two TSNNic specs are equivalent (used by the Node
// reconciler in reconciler.go to decide when a TSNNic CR needs updating).
func nicSpecEqual(a, b v1alpha1.TSNNicSpec) bool {
	return a.NodeName == b.NodeName &&
		a.IfName == b.IfName &&
		a.PCIAddress == b.PCIAddress &&
		a.MACAddress == b.MACAddress &&
		a.HostIP == b.HostIP &&
		a.TSNCapable == b.TSNCapable
}

// ParseCapableNics splits a comma-separated label value into interface names.
func ParseCapableNics(labelValue string) []string {
	if labelValue == "" {
		return nil
	}
	parts := strings.Split(labelValue, ",")
	out := make([]string, 0, len(parts))
	for _, p := range parts {
		if s := strings.TrimSpace(p); s != "" {
			out = append(out, s)
		}
	}
	return out
}
