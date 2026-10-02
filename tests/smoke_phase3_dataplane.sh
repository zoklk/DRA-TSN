#!/usr/bin/env bash
# Phase 3 smoke: NRI + eBPF data plane.
# Spec: context/phases/nri.md §8, context/phases/ebpf.md §7.
#
# Two run modes:
#  - ON a worker node (host has bpftool): checks pinned maps + attach directly.
#  - From the control plane: set NODE=<worker> to drive the checks through a
#    privileged `kubectl debug node/<worker>` pod (host bpftool via chroot).
set -u
cd "$(dirname "$0")"; . ./lib.sh
PHASE="phase3-dataplane"
: "${NODE:=}"            # if set, run host commands via kubectl debug node/<NODE>
: "${PIN:=/sys/fs/bpf/tsn}"

hdr "Phase 3 — NRI + eBPF data plane"

# hostsh "<cmd>" : run a shell command in the node's host mount namespace.
# Uses `kubectl debug node` (needs -it for reliable stdout capture); strips the
# "Creating debugging pod" banner that kubectl prints into the stream.
hostsh() {
  if [ -n "$NODE" ]; then
    command -v kubectl >/dev/null || return 1
    kubectl debug "node/$NODE" -it --image=busybox:1.36 --profile=sysadmin -- \
      chroot /host sh -c "$1" </dev/null 2>/dev/null | grep -v 'Creating debugging pod'
  else
    sh -c "$1"
  fi
}

# 0) env injection into the TSN pod (works from anywhere with kubectl).
# The NRI plugin injects a single TSN_STREAMS env var = JSON array of all bound
# streams ({id,dir,dst|port}); vlan/pcp are NOT surfaced (eBPF tags transparently).
if require kubectl; then
  POD="$(k get pods -n "$NS" -l app=vplc -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)"
  if [ -n "$POD" ]; then
    streams="$(k exec -n "$NS" "$POD" -- env 2>/dev/null | grep '^TSN_STREAMS=' | cut -d= -f2-)"
    if echo "$streams" | grep -q '"dir"'; then
      n="$(echo "$streams" | grep -o '"dir"' | wc -l | tr -d ' ')"
      pass "env TSN_STREAMS injected into $POD ($n stream(s)): $streams"
    else fail "TSN pod $POD present but TSN_STREAMS env missing/empty"; fi
  else skip "no vplc test pod in ns $NS"; fi
fi

# 1) pinned maps present
maps="$(hostsh "ls $PIN 2>/dev/null")"
if echo "$maps" | grep -q 'tx_flows'; then
  pass "bpf maps pinned under $PIN ($(echo $maps | tr '\n' ' '))"
else
  if [ -z "$NODE" ]; then skip "tx_flows not pinned on this host — run ON a worker or set NODE=<worker>"
  else fail "tx_flows not pinned under $PIN on $NODE"; fi
fi

# 2) tx_flows has an entry with a programmed vlan (tagging configured)
dump="$(hostsh "bpftool map dump pinned $PIN/tx_flows 2>/dev/null")"
if echo "$dump" | grep -q '"vlan_id"'; then
  vlan="$(echo "$dump" | grep -o '"vlan_id": [0-9]*' | head -1 | grep -o '[0-9]*')"
  pass "tx_flows programmed: vlan_id=$vlan (VLAN tagging configured for a flow)"
elif echo "$dump" | grep -q '\[\]'; then
  skip "tx_flows empty — no TSN TX pod active"
else
  skip "could not read tx_flows (need host bpftool; set NODE=<worker>)"
fi

# 3) eBPF attach points:
#    RX = tsn_rx XDP ingress on the physical TSN NIC.
#    TX = tsn_tx TCX ingress on the pod's host-side veth (pod egress -> NIC redirect),
#         no longer on the NIC; confirmed via the pinned TX links ($PIN/tcx).
net="$(hostsh "bpftool net show dev $TSN_IFACE 2>/dev/null")"
if echo "$net" | grep -q 'xdp'; then pass "RX XDP (tsn_rx) attached on $TSN_IFACE"
else
  if [ -z "$NODE" ]; then skip "no XDP visible on $TSN_IFACE — run ON the worker or set NODE"
  else fail "tsn_rx XDP not attached on $TSN_IFACE"; fi
fi
tcx="$(hostsh "ls $PIN/tcx 2>/dev/null")"
if [ -n "$tcx" ]; then
  pass "TX (tsn_tx TCX ingress) attached on host veth(s): $(echo $tcx | tr '\n' ' ')"
else
  if [ -z "$NODE" ]; then skip "no TX veth TCX links under $PIN/tcx — run ON the worker or set NODE"
  else skip "no TX veth TCX links pinned under $PIN/tcx — no TSN TX pod active"; fi
fi

# 4) RX path (best-effort; needs a fake sender)
if [ -n "$RX_ENDPOINT_IP" ]; then
  info "RX live test: send UDP to $RX_ENDPOINT_IP:$RX_ENDPOINT_PORT from a fake sender and confirm pod receipt"
fi
skip "RX live receipt + SNAT toggle capture are manual (no TSN switch in this env)"

# cleanup any debug pods we created
if [ -n "$NODE" ] && command -v kubectl >/dev/null; then
  kubectl get pods -A 2>/dev/null | grep node-debugger | awk '{print $1, $2}' | \
    while read ns nm; do kubectl delete pod -n "$ns" "$nm" --wait=false >/dev/null 2>&1; done
fi

summary
