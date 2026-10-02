package ebpf

import (
	"encoding/binary"
	"testing"
)

// The Go structs must stay byte-for-byte identical to the C map layouts in
// bpf/tsn_common.h, or every map lookup silently misses. These sizes are the
// authoritative contract (tx_key=12, tx_val=24, rx_key=12, rx_val=24).
func TestMapStructSizes(t *testing.T) {
	cases := []struct {
		name string
		v    any
		want int
	}{
		{"TXKey", TXKey{}, 12},
		{"TXVal", TXVal{}, 24},
		{"RXKey", RXKey{}, 12},
		{"RXVal", RXVal{}, 24},
	}
	for _, c := range cases {
		if got := binary.Size(c.v); got != c.want {
			t.Errorf("%s size = %d, want %d (must match struct in tsn_common.h)", c.name, got, c.want)
		}
	}
}

func TestNewTXKey(t *testing.T) {
	k, err := NewTXKey("10.244.1.5", "192.168.50.10", 4840, 17)
	if err != nil {
		t.Fatalf("NewTXKey: %v", err)
	}
	if k.SrcIP != [4]byte{10, 244, 1, 5} {
		t.Errorf("SrcIP = %v, want 10.244.1.5", k.SrcIP)
	}
	if k.DstIP != [4]byte{192, 168, 50, 10} {
		t.Errorf("DstIP = %v, want 192.168.50.10", k.DstIP)
	}
	// Port is network (big-endian) order: 4840 = 0x12E8.
	if k.DstPort != [2]byte{0x12, 0xE8} {
		t.Errorf("DstPort = %v, want network-order 4840 (0x12 0xE8)", k.DstPort)
	}
	if k.Proto != 17 {
		t.Errorf("Proto = %d, want 17", k.Proto)
	}
}

func TestNewTXValSNATToggle(t *testing.T) {
	// No SNAT IP -> flag clear, snat_src_ip zero.
	v, err := NewTXVal(200, 6, "", "02:00:00:00:50:10", "0c:c4:7a:9f:f1:ad", 7)
	if err != nil {
		t.Fatalf("NewTXVal: %v", err)
	}
	if v.Flags&TXFlagSNAT != 0 {
		t.Errorf("SNAT flag set without snatSrcIP")
	}
	if v.SnatSrcIP != [4]byte{} {
		t.Errorf("SnatSrcIP = %v, want zero", v.SnatSrcIP)
	}
	if v.VLANID != 200 || v.PCP != 6 || v.NICIfindex != 7 {
		t.Errorf("vlan/pcp/ifindex = %d/%d/%d, want 200/6/7", v.VLANID, v.PCP, v.NICIfindex)
	}
	if v.DstMAC != [6]byte{0x02, 0, 0, 0, 0x50, 0x10} {
		t.Errorf("DstMAC = %v", v.DstMAC)
	}

	// With SNAT IP -> flag set, IP populated.
	v2, err := NewTXVal(200, 6, "192.168.10.12", "02:00:00:00:50:10", "0c:c4:7a:9f:f1:ad", 7)
	if err != nil {
		t.Fatalf("NewTXVal snat: %v", err)
	}
	if v2.Flags&TXFlagSNAT == 0 {
		t.Errorf("SNAT flag not set with snatSrcIP")
	}
	if v2.SnatSrcIP != [4]byte{192, 168, 10, 12} {
		t.Errorf("SnatSrcIP = %v, want 192.168.10.12", v2.SnatSrcIP)
	}
}

func TestNewRXKeyVal(t *testing.T) {
	k, err := NewRXKey("192.168.1.20", "192.168.10.12", 4840, 17)
	if err != nil {
		t.Fatalf("NewRXKey: %v", err)
	}
	if k.SrcIP != [4]byte{192, 168, 1, 20} {
		t.Errorf("SrcIP = %v, want 192.168.1.20", k.SrcIP)
	}
	if k.OrigDstIP != [4]byte{192, 168, 10, 12} || k.DstPort != [2]byte{0x12, 0xE8} || k.Proto != 17 {
		t.Errorf("RXKey = %+v", k)
	}
	v, err := NewRXVal("10.244.1.5", 4840, "4a:bc:76:c7:d1:98", 8339, 100, "0c:c4:7a:ca:3c:3a")
	if err != nil {
		t.Fatalf("NewRXVal: %v", err)
	}
	if v.PodIP != [4]byte{10, 244, 1, 5} || v.VethIfindex != 8339 {
		t.Errorf("RXVal = %+v", v)
	}
	if v.PodMAC != [6]byte{0x4a, 0xbc, 0x76, 0xc7, 0xd1, 0x98} {
		t.Errorf("PodMAC = %v", v.PodMAC)
	}
	if v.ExpectedVID != 100 {
		t.Errorf("ExpectedVID = %d, want 100", v.ExpectedVID)
	}
	if v.TalkerMAC != [6]byte{0x0c, 0xc4, 0x7a, 0xca, 0x3c, 0x3a} {
		t.Errorf("TalkerMAC = %v", v.TalkerMAC)
	}

	// Opt-in: empty talkerMAC -> zero, expectedVID 0 -> skip.
	v0, err := NewRXVal("10.244.1.5", 4840, "4a:bc:76:c7:d1:98", 8339, 0, "")
	if err != nil {
		t.Fatalf("NewRXVal opt-out: %v", err)
	}
	if v0.ExpectedVID != 0 || v0.TalkerMAC != [6]byte{} {
		t.Errorf("opt-out RXVal = %+v", v0)
	}
}

func TestNewKeyRejectsBadInput(t *testing.T) {
	if _, err := NewTXKey("not-an-ip", "192.168.50.10", 4840, 17); err == nil {
		t.Errorf("NewTXKey accepted invalid src IP")
	}
	if _, err := NewTXVal(200, 6, "", "zz:zz", "0c:c4:7a:9f:f1:ad", 7); err == nil {
		t.Errorf("NewTXVal accepted invalid MAC")
	}
}
