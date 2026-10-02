// Package ebpf loads and attaches the DRATSN data-plane programs and exposes the
// pinned maps (tx_flows, rx_flows, xdp_devmap) for the NRI hooks to program.
package ebpf

// bpf2go compiles the C in ../../bpf into Go-embedded objects (little-endian only;
// the cluster is x86-64). Headers: vmlinux.h is committed under bpf/headers, and
// bpf_helpers.h comes from the system libbpf-dev (/usr/include/bpf).
//
//go:generate bpf2go -cc clang -target bpfel -no-global-types tx ../../bpf/tx.c -- -I../../bpf -O2 -g
//go:generate bpf2go -cc clang -target bpfel -no-global-types rx ../../bpf/rx.c -- -I../../bpf -O2 -g
//go:generate bpf2go -cc clang -target bpfel -no-global-types veth ../../bpf/veth_dummy.c -- -I../../bpf -O2 -g
