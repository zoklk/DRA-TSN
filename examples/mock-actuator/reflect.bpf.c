// Actuator-side ECHO as an XDP program — round-trip latency harness (ADR-0028).
// This is the kernel/XDP replacement for scripts/echo-ext.py: instead of an
// AF_PACKET userspace loop (subject to wakeup + GIL jitter), the actuator node
// reflects each vPLC probe frame straight back from the NIC's XDP ingress hook,
// stateless and at line rate.
//
// Runs ON A ROBOT-ARM NODE (an external "sensor/actuator"; arm-1 192.168.1.11,
// arm-2 192.168.1.12), NOT on the vPLC node and NOT as part of the DRATSN data
// plane. That is why it lives under examples/ and not in bpf/ — it is measurement
// scaffolding, loaded by hand (load.sh) with `ip link set <iface> xdp`, no maps
// and no Go loader. One build per arm (defaults = arm-1; see reflect.h for arm-2).
//
// Flow: a vPLC TX probe arrives 802.1Q-tagged (the TX data plane tagged it),
//   eth[src=w1 NIC, dst=this NIC] / IP[src=pod, dst=this node] / UDP[dst=4840] /
//   payload[magic|seq|t0].  We reflect it to the w1 RX TSN endpoint so the w1 RX
//   data plane (tsn_rx) DNATs it into the pod:
//     - L2: swap src/dst MAC  (dst <- w1 NIC, src <- this NIC)
//     - VLAN: rewrite VID/PCP to the RX stream's tag (differs from the TX tag)
//     - L3: set src=talker, dst=node endpoint  (STATIC, not a swap — rx_flows
//           keys on {talker_ip, node_endpoint_ip}; swapping would target the pod IP)
//     - L4: zero the UDP checksum (legal for IPv4); ports untouched
//     - payload (magic|seq|t0) preserved so the pod pairs the echo to its send
//   then XDP_TX bounces it out the same NIC. Non-probe frames -> XDP_PASS (normal).
#include <linux/bpf.h>
#include <linux/if_ether.h>
#include <linux/ip.h>
#include <linux/udp.h>
#include <linux/in.h>
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_endian.h>
#include "reflect.h"

char _license[] SEC("license") = "GPL";

#define ETH_P_IP_    0x0800
#define ETH_P_8021Q_ 0x8100

// 802.1Q tag (not in the uapi headers we include, so define it locally).
struct vlan_hdr {
	__be16 h_vlan_TCI;
	__be16 h_vlan_encapsulated_proto;
};

// Internet checksum over a fixed 20-byte IPv4 header (ihl==5, enforced below).
static __always_inline __u16 ipv4_csum(struct iphdr *iph)
{
	__u32 sum = 0;
	__u16 *p = (__u16 *)iph;
#pragma unroll
	for (int i = 0; i < (int)(sizeof(struct iphdr) / 2); i++)
		sum += p[i];
	sum = (sum & 0xffff) + (sum >> 16);
	sum = (sum & 0xffff) + (sum >> 16);
	return (__u16)~sum;
}

SEC("xdp")
int tsn_reflect(struct xdp_md *ctx)
{
	void *data = (void *)(long)ctx->data;
	void *data_end = (void *)(long)ctx->data_end;

	struct ethhdr *eth = data;
	if ((void *)(eth + 1) > data_end)
		return XDP_PASS;
	if (eth->h_proto != bpf_htons(ETH_P_8021Q_))
		return XDP_PASS; // TSN probe is always tagged; untagged -> normal stack

	struct vlan_hdr *vh = (void *)(eth + 1);
	if ((void *)(vh + 1) > data_end)
		return XDP_PASS;
	if (vh->h_vlan_encapsulated_proto != bpf_htons(ETH_P_IP_))
		return XDP_PASS;

	struct iphdr *iph = (void *)(vh + 1);
	if ((void *)(iph + 1) > data_end)
		return XDP_PASS;
	if (iph->ihl != 5 || iph->protocol != IPPROTO_UDP)
		return XDP_PASS; // no IP options / non-UDP -> not our probe

	struct udphdr *udph = (void *)(iph + 1);
	if ((void *)(udph + 1) > data_end)
		return XDP_PASS;

	// Probe filter: payload must start with the magic. Only then do we reflect.
	__u8 *payload = (void *)(udph + 1);
	if (payload + TS_PAYLOAD_MIN > (__u8 *)data_end)
		return XDP_PASS;
	__be32 *magic = (void *)payload;
	if (*magic != bpf_htonl(TS_MAGIC))
		return XDP_PASS;

	// --- reflect ---
	// L2: swap MAC so dst = w1 NIC (where the frame came from), src = this NIC.
	__u8 mac[6];
	__builtin_memcpy(mac, eth->h_dest, 6);
	__builtin_memcpy(eth->h_dest, eth->h_source, 6);
	__builtin_memcpy(eth->h_source, mac, 6);

	// VLAN: rewrite to the RX stream's tag (TX VID/PCP -> RX VID/PCP).
	vh->h_vlan_TCI = bpf_htons(((ECHO_PCP & 0x7) << 13) | (ECHO_VID & 0x0fff));

	// L3: static echo addressing (talker -> node endpoint), NOT a src/dst swap.
	iph->saddr = bpf_htonl(ECHO_SRC_IP);
	iph->daddr = bpf_htonl(ECHO_DST_IP);
	iph->check = 0;
	iph->check = ipv4_csum(iph);

	// L4: retarget the dst port to this arm's RX node-endpoint port (TX listener
	// port != RX port when arms are split, e.g. arm-2 = 4841), then zero the UDP
	// checksum (valid for IPv4) so no pseudo-header recompute is needed. The src
	// port is irrelevant to the rx_flows key, so leave it.
	udph->dest = bpf_htons(ECHO_DPORT);
	udph->check = 0;

	return XDP_TX; // bounce out the same NIC
}
