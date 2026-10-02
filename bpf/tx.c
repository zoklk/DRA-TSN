// TX data plane: TCX HEAD ingress on the pod's host-side veth (= pod egress). For
// packets matching a tx_flows entry it optionally SNATs the source, rebuilds L2
// for the TSN segment (src=NIC MAC, dst=listener MAC), pushes an 802.1Q VLAN/PCP
// tag, and bpf_redirect()s straight to the physical TSN NIC — bypassing host
// routing/netfilter/conntrack/cilium entirely (full host-ns isolation for the
// TSN flow), while still going through the NIC qdisc (taprio/etf compatible, as
// redirect uses the skb dev_queue_xmit path, not XDP's ndo_xdp_xmit). Non-matching
// packets return TCX_NEXT so cilium's from-container handles them (selective bypass).
#include "headers/vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_endian.h>
#include "tsn_common.h"

char _license[] SEC("license") = "GPL";

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__type(key, struct tx_key);
	__type(value, struct tx_val);
	__uint(max_entries, 1024);
	__uint(pinning, LIBBPF_PIN_BY_NAME);
} tx_flows SEC(".maps");

// Latency-measurement timeline, keyed by probe seq (ADR-0028). tsn_tx writes t1
// (veth ingress), tsn_tx_guard writes t2 (NIC egress). ARRAY so `bpftool map dump`
// can read it from a shell; pinned to persist across the process.
struct {
	__uint(type, BPF_MAP_TYPE_ARRAY);
	__type(key, __u32);
	__type(value, struct tx_ts_sample);
	__uint(max_entries, TS_MAX_SAMPLES);
	__uint(pinning, LIBBPF_PIN_BY_NAME);
} tx_ts SEC(".maps");

SEC("tcx/ingress")
int tsn_tx(struct __sk_buff *skb)
{
	void *data = (void *)(long)skb->data;
	void *data_end = (void *)(long)skb->data_end;

	struct ethhdr *eth = data;
	if ((void *)(eth + 1) > data_end)
		return TCX_NEXT;
	if (eth->h_proto != bpf_htons(ETH_P_IP))
		return TCX_NEXT;

	struct iphdr *iph = (void *)(eth + 1);
	if ((void *)(iph + 1) > data_end)
		return TCX_NEXT;
	if (iph->protocol != IPPROTO_UDP)
		return TCX_NEXT;

	__u32 ihl = iph->ihl * 4;
	if (ihl < sizeof(struct iphdr))
		return TCX_NEXT;
	struct udphdr *udph = (void *)iph + ihl;
	if ((void *)(udph + 1) > data_end)
		return TCX_NEXT;

	struct tx_key key = {
		.src_ip   = iph->saddr,
		.dst_ip   = iph->daddr,
		.dst_port = udph->dest,
		.proto    = iph->protocol,
	};
	struct tx_val *val = bpf_map_lookup_elem(&tx_flows, &key);
	if (!val)
		return TCX_NEXT; // not a TSN stream -> cilium normal path

	// Measurement: extract the probe seq now, before the frame edits below
	// invalidate the data pointers. The t1 timestamp is taken just before redirect.
	__u32 ts_seq;
	int ts_ok = ts_probe_seq((void *)(udph + 1), data_end, &ts_seq);

	// Optional static SNAT: rewrite src IP before tagging. Offsets are computed
	// from skb so the helpers fix up checksums correctly.
	if (val->flags & TX_FLAG_SNAT) {
		__be32 old_ip = iph->saddr;
		__be32 new_ip = val->snat_src_ip;
		int ip_off = (void *)iph - data;
		int saddr_off = ip_off + offsetof(struct iphdr, saddr);

		bpf_l3_csum_replace(skb, ip_off + offsetof(struct iphdr, check), old_ip, new_ip, sizeof(new_ip));
		// UDP checksum uses a pseudo-header that includes the src IP.
		int udp_off = (void *)udph - data;
		bpf_l4_csum_replace(skb, udp_off + offsetof(struct udphdr, check), old_ip, new_ip,
				    BPF_F_PSEUDO_HDR | sizeof(new_ip));
		bpf_skb_store_bytes(skb, saddr_off, &new_ip, sizeof(new_ip), 0);
	}

	// Rebuild L2 for the TSN segment: the pod addressed its default gateway
	// (src=pod MAC, dst=veth/gw MAC), but on the TSN bridge the talker is the
	// physical NIC and the frame must carry the listener's MAC as destination.
	// store_bytes (not direct write) so the helper re-validates skb bounds.
	bpf_skb_store_bytes(skb, offsetof(struct ethhdr, h_dest), val->dst_mac, 6, 0);
	bpf_skb_store_bytes(skb, offsetof(struct ethhdr, h_source), val->src_mac, 6, 0);

	// Push 802.1Q with vlan id + pcp. At this hook bpf_skb_vlan_push only sets skb
	// vlan metadata (vlan_tci/proto), bytes stay plain IP; but the bpf_redirect below
	// crosses a device boundary to the NIC, where the kernel flushes the tag inline,
	// so on the wire it is a real 802.1Q frame (ethertype 0x8100 -- verified on eno6).
	// cilium's egress program drops VLAN frames ("VLAN traffic disallowed"), so we
	// mark the skb and let the NIC-egress guard (tsn_tx_guard) TCX_PASS it past cilium.
	__u16 vlan_tci = (val->pcp & 0x7) << 13 | (val->vlan_id & 0x0fff);
	bpf_skb_vlan_push(skb, bpf_htons(ETH_P_8021Q), vlan_tci);
	skb->mark = TSN_TX_MARK;
	// ADR-0008: mirror the PCP into skb->priority (kernel-internal, never on the
	// wire) so the NIC's mqprio/taprio qdisc classifies the flow into its TC.
	// Survives the bpf_redirect below to the NIC's dev_queue_xmit.
	skb->priority = val->pcp;

	// t1: veth ingress timestamp (probe frames only).
	if (ts_ok) {
		struct tx_ts_sample *ts = bpf_map_lookup_elem(&tx_ts, &ts_seq);
		if (ts)
			ts->t1 = bpf_ktime_get_tai_ns();
	}

	// Redirect to the physical NIC egress (skb path -> dev_queue_xmit -> qdisc).
	return bpf_redirect(val->nic_ifindex, 0);
}

// NIC-egress guard: TCX HEAD on the physical TSN NIC's egress. TSN frames the veth
// program redirected here carry TSN_TX_MARK -> terminate the chain (TCX_PASS) so
// they bypass cilium's cil_to_netdev VLAN filter and reach the wire (HW inserts the
// 802.1Q tag). All other egress traffic falls through (TCX_NEXT) to cilium intact.
SEC("tcx/egress")
int tsn_tx_guard(struct __sk_buff *skb)
{
	if (skb->mark != TSN_TX_MARK)
		return TCX_NEXT; // non-TSN egress -> cilium intact (no payload parse)

	// t2: NIC egress timestamp (probe frames only). Only TSN frames reach here
	// (mark gate above), so the payload parse never touches normal traffic. The
	// 802.1Q tag is still skb metadata at this point, so bytes are eth+ip+udp+data.
	void *data = (void *)(long)skb->data;
	void *data_end = (void *)(long)skb->data_end;
	struct ethhdr *eth = data;
	if ((void *)(eth + 1) <= data_end && eth->h_proto == bpf_htons(ETH_P_IP)) {
		struct iphdr *iph = (void *)(eth + 1);
		if ((void *)(iph + 1) <= data_end && iph->protocol == IPPROTO_UDP && iph->ihl == 5) {
			struct udphdr *udph = (void *)(iph + 1);
			if ((void *)(udph + 1) <= data_end) {
				__u32 seq;
				if (ts_probe_seq((void *)(udph + 1), data_end, &seq)) {
					struct tx_ts_sample *ts = bpf_map_lookup_elem(&tx_ts, &seq);
					if (ts)
						ts->t2 = bpf_ktime_get_tai_ns();
				}
			}
		}
	}
	return TCX_PASS;
}
