// Dummy XDP program attached to the pod-side veth. It does nothing but
// XDP_PASS; its sole purpose is to enable the napi/XDP receive path on the veth
// so that bpf_redirect_map() from the physical NIC's XDP program can land frames
// on this veth (a kernel veth requirement for native XDP redirect targets).
#include "headers/vmlinux.h"
#include <bpf/bpf_helpers.h>

char _license[] SEC("license") = "GPL";

SEC("xdp")
int tsn_veth_dummy(struct xdp_md *ctx)
{
	return XDP_PASS;
}
