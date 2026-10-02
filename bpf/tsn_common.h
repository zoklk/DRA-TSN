// Shared map key/value layout for the DRATSN data plane. Kept byte-for-byte in
// sync with the Go structs in pkg/ebpf (maps_types.go). All multi-byte network
// fields (IPs, ports) are stored in network byte order in the maps, so the Go
// loader must insert them the same way.
#ifndef __TSN_COMMON_H__
#define __TSN_COMMON_H__

// TCX (tc express) return codes. NOTE these differ from classic tc TC_ACT_*:
//   TCX_NEXT(-1) = run the next program in the chain (e.g. cilium cil_to_netdev)
//   TCX_PASS(0)  = terminate the chain AND pass (== TC_ACT_OK) -> bypasses cilium
#define TCX_NEXT -1
#define TCX_PASS 0
#define TCX_DROP 2

#define ETH_P_IP    0x0800
#define ETH_P_8021Q 0x8100
#define IPPROTO_UDP 17

// tx_flows value flags.
#define TX_FLAG_SNAT 0x1 // bit0: rewrite src IP to snat_src_ip before vlan_push

// skb->mark stamped by the veth TX program onto redirected TSN frames. The NIC
// egress guard (tsn_tx_guard) matches it to TCX_PASS them past cilium's
// cil_to_netdev, which otherwise drops them ("VLAN traffic disallowed by VLAN
// filter"). Picked to avoid cilium's mark magics (low byte != cilium values).
#define TSN_TX_MARK 0x0000DA75

// tx_flows key: 4-tuple {src_ip(pod), dst_ip, dst_port, proto}. src_port is
// deliberately excluded (unknown at NRI insert time; same stream regardless).
struct tx_key {
	__be32 src_ip;
	__be32 dst_ip;
	__be16 dst_port;
	__u8   proto;
	__u8   _pad;
};

// tx_val: vlan/pcp + optional SNAT + L2 build + redirect target. The TX program
// runs at the host-side veth ingress (pod egress) and rebuilds the frame for the
// TSN segment: src_mac=NIC, dst_mac=listener (TSN is an L2 bridge, so dst is the
// final end-station MAC, not a per-hop router MAC), then bpf_redirect to the NIC.
// Field order is alignment-stable (4B fields first) to match the Go struct.
struct tx_val {
	__be32 snat_src_ip;
	__u32  nic_ifindex;   // bpf_redirect target (physical TSN NIC)
	__u16  vlan_id;
	__u8   pcp;
	__u8   flags;
	__u8   dst_mac[6];    // = listener (end-station) MAC, from CNC
	__u8   src_mac[6];    // = NIC MAC (talker endpoint on the TSN segment)
};

// rx_flows key: byte-for-byte identical to tx_key (ADR-0027) — RX identifies the
// stream by sender(talker) + endpoint + proto, symmetric with TX. src_ip is the
// talker (sensor) source; it is part of the key, so the entry MUST carry the real
// talker IP (no 0.0.0.0 "skip" like the value fields — a hash lookup would never
// match a wildcard against the packet's real source).
struct rx_key {
	__be32 src_ip;       // talker (sensor) source IP
	__be32 orig_dst_ip;  // node TSN NIC endpoint IP
	__be16 dst_port;
	__u8   proto;
	__u8   _pad;
};

// rx_val: DNAT target (pod) + optional stream-identification hardening fields.
// 4-byte fields first for alignment-stable layout (mirrors the Go struct). The
// expected_vid / talker_mac fields are opt-in: 0 = skip that check (isolated-net
// demo backward-compat), non-zero = enforce it.
struct rx_val {
	__be32 pod_ip;        // DNAT dst IP
	__u32  veth_ifindex;  // redirect target (also the xdp_devmap key)
	__be16 pod_port;      // DNAT dst port
	__u16  expected_vid;  // native; 0 = skip on-wire VID check, else must match
	__u8   pod_mac[6];    // dst MAC rewritten to the pod's eth0 so the pod stack
	                      // accepts the frame (without this it drops on MAC mismatch)
	__u8   talker_mac[6]; // 0 = skip L2 sender check, else frame src MAC must match
};

// --- Latency-measurement instrumentation (ADR-0028) ---
// A userspace probe stamps each UDP payload with [magic][seq][t0] so the data
// plane can timestamp the SAME packet at each hop, keyed by seq. Cost on non-probe
// traffic = one magic compare then skip (only on map-hit TSN frames). seq is the
// index into a per-direction ARRAY map (tx_ts/rx_ts), dumped by `bpftool map dump`.
#define TS_MAGIC       0x54534E31u // "TSN1" (big-endian on the wire)
#define TS_MAX_SAMPLES 65536       // seq = array index; the probe wraps seq mod this
#define TS_PAYLOAD_MIN 16          // magic(4) + seq(4) + t0(8)

// tx_ts value: per-packet TX timeline. t1 = veth TCX ingress (tsn_tx),
// t2 = NIC egress (tsn_tx_guard). TAI nanoseconds (bpf_ktime_get_tai_ns).
struct tx_ts_sample { __u64 t1; __u64 t2; };
// rx_ts value: t3 = NIC ingress (tsn_rx), TAI nanoseconds.
struct rx_ts_sample { __u64 t3; };

// ts_probe_seq: if payload carries the probe magic, set *seq (host order) and
// return 1; else 0. Bounds-checked for the verifier. payload = start of UDP data.
// Requires bpf_endian.h (included before this header in tx.c/rx.c).
static __always_inline int ts_probe_seq(void *payload, void *data_end, __u32 *seq)
{
	if (payload + TS_PAYLOAD_MIN > data_end)
		return 0;
	__be32 *p = payload;
	if (p[0] != bpf_htonl(TS_MAGIC))
		return 0;
	__u32 s = bpf_ntohl(p[1]);
	if (s >= TS_MAX_SAMPLES)
		return 0;
	*seq = s;
	return 1;
}

#endif // __TSN_COMMON_H__
