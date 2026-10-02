// RX data plane: XDP ingress on the physical TSN NIC. For packets whose
// {dst_ip,dst_port} match an rx_flows entry (the node's static TSN receive
// endpoint), it DNATs the destination to the target pod and redirects the frame
// straight to the pod veth via a devmap — bypassing the kernel stack, kube-proxy
// and conntrack (deterministic ingress). Non-matching packets XDP_PASS to cilium.
//
// Frame edits: strip the single 802.1Q tag, DNAT L3/L4 (dst IP+port -> pod), and
// rewrite the L2 dst MAC to the pod's eth0 so its stack accepts the frame. The src
// MAC is left as-is (the pod replies via ARP). Assumes one VLAN tag + no IP options
// (ihl==5); QinQ / option-bearing headers fall through to cilium (XDP_PASS).
#include "headers/vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_endian.h>
#include "tsn_common.h"

char _license[] SEC("license") = "GPL";

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__type(key, struct rx_key);
	__type(value, struct rx_val);
	__uint(max_entries, 1024);
	__uint(pinning, LIBBPF_PIN_BY_NAME);
} rx_flows SEC(".maps");

// DEVMAP_HASH (not DEVMAP array): keyed by veth ifindex, which on a busy node
// exceeds any fixed array size (observed 7784). HASH allows sparse u32 keys up
// to max_entries distinct entries regardless of the key value.
struct {
	__uint(type, BPF_MAP_TYPE_DEVMAP_HASH);
	__type(key, __u32);
	__type(value, __u32);
	__uint(max_entries, 1024);
	__uint(pinning, LIBBPF_PIN_BY_NAME);
} xdp_devmap SEC(".maps");

// Latency-measurement timeline, keyed by probe seq (ADR-0028). tsn_rx writes t3
// (NIC ingress). ARRAY so `bpftool map dump` reads it from a shell; pinned.
struct {
	__uint(type, BPF_MAP_TYPE_ARRAY);
	__type(key, __u32);
	__type(value, struct rx_ts_sample);
	__uint(max_entries, TS_MAX_SAMPLES);
	__uint(pinning, LIBBPF_PIN_BY_NAME);
} rx_ts SEC(".maps");

static __always_inline __u16 csum_fold(__u32 csum)
{
	csum = (csum & 0xffff) + (csum >> 16);
	csum = (csum & 0xffff) + (csum >> 16);
	return (__u16)~csum;
}

// Incremental ones-complement update for a changed 32-bit field (RFC 1624).
static __always_inline void csum_replace4(__u16 *check, __be32 old, __be32 neu)
{
	__u16 *o = (__u16 *)&old;
	__u16 *n = (__u16 *)&neu;
	__u32 sum = (~(*check) & 0xffff);
	sum += (~o[0] & 0xffff) + (~o[1] & 0xffff);
	sum += n[0] + n[1];
	*check = csum_fold(sum);
}

// Incremental update for a changed 16-bit field.
static __always_inline void csum_replace2(__u16 *check, __be16 old, __be16 neu)
{
	__u32 sum = (~(*check) & 0xffff) + (~old & 0xffff) + neu;
	*check = csum_fold(sum);
}

SEC("xdp")
int tsn_rx(struct xdp_md *ctx)
{
	void *data = (void *)(long)ctx->data;
	void *data_end = (void *)(long)ctx->data_end;

	struct ethhdr *eth = data;
	if ((void *)(eth + 1) > data_end)
		return XDP_PASS;

	// TSN classification: a TSN RX frame is 802.1Q-tagged (the switch tags the
	// stream). Untagged traffic to the same node endpoint is NOT a TSN stream ->
	// hand it to the normal stack / cilium (XDP_PASS), never DNAT it.
	if (eth->h_proto != bpf_htons(ETH_P_8021Q))
		return XDP_PASS;

	if (data + 16 > data_end) // dst6+src6+tpid2+tci2+inner2
		return XDP_PASS;

	// Capture identification fields BEFORE stripping the tag (ADR-0027): the on-wire
	// VID (low 12 bits of the TCI) and the L2 sender MAC. Validated after the
	// rx_flows lookup so a tagged-but-wrong-VID or spoofed-sender frame to the same
	// endpoint falls through to cilium instead of being DNATed to the pod.
	struct vlan_hdr *vh = (void *)(eth + 1);
	__u16 onwire_vid = bpf_ntohs(vh->h_vlan_TCI) & 0x0fff;
	__u8 src_mac[6];
	__builtin_memcpy(src_mac, eth->h_source, 6);

	// Parse the inner headers THROUGH the tag, without removing it yet. Stripping
	// first would be cheaper, but every XDP_PASS below this point is a frame we
	// are handing back to cilium, and handing it back de-tagged breaks the
	// per-flow bypass contract (ADR-0001: a map miss must be indistinguishable
	// from normal traffic). A tagged frame that misses the map, carries the wrong
	// VID, or is not IPv4/UDP has to reach the stack exactly as it arrived.
	if (vh->h_vlan_encapsulated_proto != bpf_htons(ETH_P_IP))
		return XDP_PASS;

	struct iphdr *iph = (void *)(vh + 1);
	if ((void *)(iph + 1) > data_end)
		return XDP_PASS;
	if (iph->protocol != IPPROTO_UDP || iph->ihl != 5)
		return XDP_PASS;

	struct udphdr *udph = (void *)(iph + 1);
	if ((void *)(udph + 1) > data_end)
		return XDP_PASS;

	struct rx_key key = {
		.src_ip      = iph->saddr,
		.orig_dst_ip = iph->daddr,
		.dst_port    = udph->dest,
		.proto       = iph->protocol,
	};
	struct rx_val *val = bpf_map_lookup_elem(&rx_flows, &key);
	if (!val)
		return XDP_PASS; // not a TSN receive endpoint -> cilium normal path

	// Stream-identification hardening (ADR-0027). On-wire VID must match the
	// stream's vlanId (when set); the frame's L2 sender must match the talker MAC
	// (when set). Mismatch -> hand to cilium (XDP_PASS), never DNAT.
	if (val->expected_vid && onwire_vid != val->expected_vid)
		return XDP_PASS;
	if (val->talker_mac[0] | val->talker_mac[1] | val->talker_mac[2] |
	    val->talker_mac[3] | val->talker_mac[4] | val->talker_mac[5]) {
		#pragma unroll
		for (int i = 0; i < 6; i++) {
			if (src_mac[i] != val->talker_mac[i])
				return XDP_PASS;
		}
	}

	// The frame is ours: only now strip the single 802.1Q tag, since the pod's
	// eth0 expects untagged. Shift dst+src MAC (12B) forward over the TPID/TCI,
	// then shrink the head by 4B; the inner ethertype already sits in the right
	// place afterwards.
	__builtin_memmove(data + 4, data, 12);
	if (bpf_xdp_adjust_head(ctx, 4))
		return XDP_DROP; // head already shifted: the frame is no longer what
		                 // arrived, so handing it to the stack is worse than
		                 // dropping it
	data = (void *)(long)ctx->data;
	data_end = (void *)(long)ctx->data_end;
	eth = data;
	if ((void *)(eth + 1) > data_end)
		return XDP_DROP;
	iph = (void *)(eth + 1);
	if ((void *)(iph + 1) > data_end)
		return XDP_DROP;
	udph = (void *)(iph + 1);
	if ((void *)(udph + 1) > data_end)
		return XDP_DROP;

	__be32 old_dst = iph->daddr;
	__be16 old_port = udph->dest;

	// DNAT: node endpoint -> pod endpoint.
	iph->daddr = val->pod_ip;
	udph->dest = val->pod_port;

	csum_replace4(&iph->check, old_dst, val->pod_ip);
	if (udph->check != 0) {
		csum_replace4(&udph->check, old_dst, val->pod_ip); // pseudo-header
		csum_replace2(&udph->check, old_port, val->pod_port);
	}

	// Rewrite L2 dst to the pod's eth0 MAC so the pod stack accepts the frame
	// after redirect. No checksum to fix (Ethernet FCS is recomputed in hardware).
	__builtin_memcpy(eth->h_dest, val->pod_mac, 6);

	// t3: NIC ingress timestamp (probe frames only). DNAT above did not touch the
	// payload, so the seq is still at the start of the UDP data.
	__u32 ts_seq;
	if (ts_probe_seq((void *)(udph + 1), data_end, &ts_seq)) {
		struct rx_ts_sample *ts = bpf_map_lookup_elem(&rx_ts, &ts_seq);
		if (ts)
			ts->t3 = bpf_ktime_get_tai_ns();
	}

	return bpf_redirect_map(&xdp_devmap, val->veth_ifindex, 0);
}
