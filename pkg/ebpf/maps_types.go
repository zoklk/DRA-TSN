package ebpf

import (
	"encoding/binary"
	"fmt"
	"net"
)

// These Go structs mirror the C map layouts in bpf/tsn_common.h byte-for-byte.
// IP/port fields are byte arrays holding network-order bytes (the BPF programs
// read them straight from packets); vlan/ifindex are native-endian integers
// (used as plain integers in the C code). cilium/ebpf marshals structs with
// native endianness, so byte arrays pass through verbatim.

// TXKey is tx_flows key: 4-tuple {src_ip, dst_ip, dst_port, proto}.
type TXKey struct {
	SrcIP   [4]byte
	DstIP   [4]byte
	DstPort [2]byte
	Proto   uint8
	Pad     uint8
}

// TXVal is tx_flows value. Field order matches struct tx_val in tsn_common.h
// (4-byte fields first for alignment-stable layout).
type TXVal struct {
	SnatSrcIP  [4]byte
	NICIfindex uint32 // bpf_redirect target (physical TSN NIC)
	VLANID     uint16
	PCP        uint8
	Flags      uint8
	DstMAC     [6]byte // listener (end-station) MAC
	SrcMAC     [6]byte // NIC MAC (talker endpoint)
}

// RXKey is rx_flows key: byte-for-byte identical to TXKey (ADR-0027) — the stream
// is identified by talker(src) + endpoint + proto, symmetric with TX.
type RXKey struct {
	SrcIP     [4]byte // talker (sensor) source IP
	OrigDstIP [4]byte // node TSN NIC endpoint IP
	DstPort   [2]byte
	Proto     uint8
	Pad       uint8
}

// RXVal is rx_flows value: DNAT target (pod) + opt-in identification hardening.
// Field order matches struct rx_val in tsn_common.h (4-byte fields first).
// ExpectedVID==0 / TalkerMAC zero = skip that check (backward-compat).
type RXVal struct {
	PodIP       [4]byte
	VethIfindex uint32
	PodPort     [2]byte
	ExpectedVID uint16  // native; 0 = skip on-wire VID check
	PodMAC      [6]byte // dst MAC the RX program rewrites to (pod eth0)
	TalkerMAC   [6]byte // 0 = skip L2 sender check
}

// TX flags.
const TXFlagSNAT uint8 = 0x1

// Latency-measurement timeline samples (ADR-0028), mirroring struct tx_ts_sample /
// rx_ts_sample in tsn_common.h. Indexed by probe seq in the tx_ts / rx_ts ARRAY
// maps. Timestamps are TAI nanoseconds (bpf_ktime_get_tai_ns); 0 = not stamped.
// Collection is normally via `bpftool map dump`; these mirrors let Go read them too.
type TXTsSample struct {
	T1 uint64 // veth TCX ingress (tsn_tx)
	T2 uint64 // NIC egress (tsn_tx_guard)
}
type RXTsSample struct {
	T3 uint64 // NIC ingress (tsn_rx)
}

// TSMagic / TSMaxSamples mirror tsn_common.h; the probe stamps payloads with
// [TSMagic][seq][t0] and wraps seq modulo TSMaxSamples.
const (
	TSMagic      uint32 = 0x54534E31 // "TSN1"
	TSMaxSamples uint32 = 65536
)

func ip4(s string) ([4]byte, error) {
	var b [4]byte
	ip := net.ParseIP(s)
	if ip == nil {
		return b, fmt.Errorf("invalid IP %q", s)
	}
	v4 := ip.To4()
	if v4 == nil {
		return b, fmt.Errorf("not IPv4: %q", s)
	}
	copy(b[:], v4)
	return b, nil
}

func port2(p uint16) [2]byte {
	var b [2]byte
	binary.BigEndian.PutUint16(b[:], p) // network order
	return b
}

// NewTXKey builds a tx_flows key from string IPs and a port (UDP assumed).
func NewTXKey(srcIP, dstIP string, dstPort uint16, proto uint8) (TXKey, error) {
	var k TXKey
	s, err := ip4(srcIP)
	if err != nil {
		return k, fmt.Errorf("src: %w", err)
	}
	d, err := ip4(dstIP)
	if err != nil {
		return k, fmt.Errorf("dst: %w", err)
	}
	k.SrcIP, k.DstIP, k.DstPort, k.Proto = s, d, port2(dstPort), proto
	return k, nil
}

// NewTXVal builds a tx_flows value. dstMAC is the listener (end-station) MAC the
// frame is rebuilt with; srcMAC is the NIC MAC; nicIfindex is the bpf_redirect
// target. If snatSrcIP is non-empty the SNAT flag is set.
func NewTXVal(vlanID uint16, pcp uint8, snatSrcIP, dstMAC, srcMAC string, nicIfindex uint32) (TXVal, error) {
	v := TXVal{VLANID: vlanID, PCP: pcp, NICIfindex: nicIfindex}
	d, err := mac6(dstMAC)
	if err != nil {
		return v, fmt.Errorf("dst mac: %w", err)
	}
	s, err := mac6(srcMAC)
	if err != nil {
		return v, fmt.Errorf("src mac: %w", err)
	}
	v.DstMAC, v.SrcMAC = d, s
	if snatSrcIP != "" {
		ip, err := ip4(snatSrcIP)
		if err != nil {
			return v, fmt.Errorf("snat: %w", err)
		}
		v.SnatSrcIP = ip
		v.Flags |= TXFlagSNAT
	}
	return v, nil
}

// NewRXKey builds an rx_flows key from the talker(src) IP, node endpoint IP:port
// and proto (ADR-0027). srcIP must be the real talker IP — it is part of the key.
func NewRXKey(srcIP, origDstIP string, dstPort uint16, proto uint8) (RXKey, error) {
	var k RXKey
	s, err := ip4(srcIP)
	if err != nil {
		return k, fmt.Errorf("src: %w", err)
	}
	d, err := ip4(origDstIP)
	if err != nil {
		return k, fmt.Errorf("dst: %w", err)
	}
	k.SrcIP, k.OrigDstIP, k.DstPort, k.Proto = s, d, port2(dstPort), proto
	return k, nil
}

// NewRXVal builds an rx_flows value pointing at a pod endpoint + veth ifindex.
// podMAC is the pod's eth0 MAC; the RX program rewrites the frame's dst MAC to it
// so the pod stack accepts the redirected frame. expectedVID (0 = skip) and
// talkerMAC ("" = skip) are the opt-in stream-identification hardening fields.
func NewRXVal(podIP string, podPort uint16, podMAC string, vethIfindex uint32, expectedVID uint16, talkerMAC string) (RXVal, error) {
	var v RXVal
	p, err := ip4(podIP)
	if err != nil {
		return v, err
	}
	m, err := mac6(podMAC)
	if err != nil {
		return v, fmt.Errorf("pod mac: %w", err)
	}
	v.PodIP, v.PodPort, v.PodMAC, v.VethIfindex = p, port2(podPort), m, vethIfindex
	v.ExpectedVID = expectedVID
	if talkerMAC != "" {
		tm, err := mac6(talkerMAC)
		if err != nil {
			return v, fmt.Errorf("talker mac: %w", err)
		}
		v.TalkerMAC = tm
	}
	return v, nil
}

func mac6(s string) ([6]byte, error) {
	var b [6]byte
	hw, err := net.ParseMAC(s)
	if err != nil {
		return b, fmt.Errorf("invalid MAC %q: %w", s, err)
	}
	if len(hw) != 6 {
		return b, fmt.Errorf("not a 6-byte MAC: %q", s)
	}
	copy(b[:], hw)
	return b, nil
}
