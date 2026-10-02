#!/usr/bin/env bash
# fig_tagging: per-flow selective bypass — OT and IT flows sent CONCURRENTLY
# (interleaved) over the same path, classified per packet afterwards.
#
#   TX mix: pod alternates :4840 (OT, registered 4-tuple) / :9999 (IT, map miss)
#           -> single eno1 egress capture. Expect OT 100% vlan TX_VLAN/pcp TX_PCP,
#              IT 100% untagged (cilium/host path).
#   RX mix: peer injects vlan RX_VLAN/pcp5 alternating talker ARM1_IP (OT,
#           rx_flows hit) / 192.168.1.99 (IT, miss).
#           OT: XDP DNAT+redirect -> visible at XDP entry + pod eth0 (DNATed),
#               invisible to eno1 tcpdump (consumed pre-stack).
#           IT: XDP_PASS -> visible at XDP entry + eno1 tcpdump, absent in pod.
#
# Capture points: w1 eno1 tcpdump (-Q out / -Q in), w1 xdpdump (XDP entry),
# pod eth0 tcpdump (netshoot). Peer injection from INJECT_NODE.
#
# Output: results/<RUN>/tx_mix.pcap rx_{wire,xdp,pod}.pcap meta.env
# Then:   ./parse.sh results/<RUN>  ->  summary.csv + packets.csv
# Usage:  FRAMES=200 ./run.sh    (FRAMES = per flow class)
set -u
cd "$(dirname "$0")"
. ../lib.sh
PHASE="fig_tagging"
: "${RUN:=$(date +%Y%m%d-%H%M%S)}"
: "${CAP_SECS:=25}"          # capture window per sub-scenario
OUT="results/$RUN"; mkdir -p "$OUT"
RTMP=/tmp/exp-tagging        # remote scratch dir on nodes

hdr "fig_tagging (FRAMES=$FRAMES -> $OUT)"

require kubectl || { summary; exit 1; }
require tshark  || { summary; exit 1; }
ensure_ns
cnc_pf_start
ensure_streams
cnc_pf_stop

NICMAC="$(jget '{.spec.macAddress}' tsnnic "$TSN_NODE-$TSN_IFACE")"
[ -n "$NICMAC" ] || { fail "missing NICMAC"; summary; exit 1; }
node_ssh "$TSN_NODE_IP" "mkdir -p $RTMP"
node_ssh "$INJECT_NODE_IP" "mkdir -p $RTMP"
node_scp inject.py "$SSH_USER@$INJECT_NODE_IP:$RTMP/inject.py"

map_len() { # <map> : entry count of a pinned bpf map on TSN_NODE
  node_sudo "$TSN_NODE_IP" "bpftool map dump pinned /sys/fs/bpf/tsn/$1 -j" \
    | "$EXP_PY" -c 'import json,sys;print(len(json.load(sys.stdin)))'
}
deploy_fixture() {
  if ! resource_exists pod mock-vplc -n "$NS"; then
    info "deploying mock-vplc fixture"
    k apply -f ../../deployments/examples/mock-vplc.yaml >/dev/null
  fi
  k wait --for=condition=Ready pod/mock-vplc -n "$NS" --timeout=120s >/dev/null \
    || { fail "mock-vplc not Ready"; summary; exit 1; }
  sleep 3   # NRI map inserts settle
  POD_IP="$(jget '{.status.podIP}' pod mock-vplc -n "$NS")"
  [ -n "$POD_IP" ] || { fail "missing POD_IP"; summary; exit 1; }
  local nflows; nflows="$(map_len tx_flows)"
  [ "$nflows" -ge 1 ] || { fail "tx_flows empty — NRI did not program the maps"; summary; exit 1; }
  info "pod=$POD_IP nic=$NICMAC tx_flows=$nflows"
}

# w1_capture <file> <in|out> <bpf filter> : background tcpdump on eno1 (CAP_SECS)
w1_capture() {
  node_sudo "$TSN_NODE_IP" \
    "timeout $CAP_SECS tcpdump -i $TSN_IFACE -Q $2 -nn -w $RTMP/$1 '$3' >/dev/null 2>&1; chmod 644 $RTMP/$1" &
}
w1_xdpdump() { # <file>
  node_sudo "$TSN_NODE_IP" \
    "timeout $CAP_SECS xdpdump -i $TSN_IFACE -w $RTMP/$1 >/dev/null 2>&1; chmod 644 $RTMP/$1" &
}
pod_capture() { # <file>
  k exec -n "$NS" mock-vplc -- sh -c \
    "timeout $CAP_SECS tcpdump -i eth0 -nn -w /tmp/$1 'udp' >/dev/null 2>&1" &
}
# interleaved OT/IT sender: even i -> registered :4840 (OT), odd i -> :9999 (IT).
# Truly concurrent flows in ONE capture — per-packet fate is then split by port.
send_tx_mix() {
  k exec -n "$NS" mock-vplc -- bash -c \
    "i=0; while [ \$i -lt $((FRAMES * 2)) ]; do p=4840; [ \$((i % 2)) = 1 ] && p=9999; printf 'DRATSN-TAG-%06d' \$i > /dev/udp/$ARM1_IP/\$p; i=\$((i+1)); sleep 0.01; done" \
    >/dev/null 2>&1
}
inject_rx_mix() { # alternate registered talker (OT) / unknown talker (IT)
  node_sudo "$INJECT_NODE_IP" \
    "python3 $RTMP/inject.py $TSN_IFACE $NICMAC $RX_VLAN 5 $ARM1_IP,$IT_TALKER $RX_ENDPOINT_IP $RX_ENDPOINT_PORT $FRAMES"
}
fetch() { node_scp "$SSH_USER@$TSN_NODE_IP:$RTMP/$1" "$OUT/$1"; }
IT_TALKER=192.168.1.99

# --- RX baseline (ablation "(a) without DRATSN"): rx_flows must be EMPTY, so
# the fixture pod holding the stream claims is removed first. Injected frames
# then all XDP_PASS into the host stack; nothing is DNATed to any pod. ---
hdr "RX baseline: inject with rx_flows empty (no claimed pod)"
if resource_exists pod mock-vplc -n "$NS"; then
  info "deleting mock-vplc (must clear rx_flows for the ablation)"
  k delete pod mock-vplc -n "$NS" --wait=true >/dev/null
  wait_for 60 claims_gone || { fail "claims not released"; summary; exit 1; }
  sleep 2
fi
rxn="$(map_len rx_flows)"
[ "$rxn" -eq 0 ] || { fail "rx_flows still has $rxn entries after pod removal"; summary; exit 1; }
w1_capture rxbase_wire.pcap in "(udp dst port $RX_ENDPOINT_PORT) or (vlan and udp dst port $RX_ENDPOINT_PORT)"
w1_xdpdump rxbase_xdp.pcap
sleep 3; inject_rx_mix; wait
fetch rxbase_wire.pcap; fetch rxbase_xdp.pcap

# --- TX baseline (ablation "(a) without DRATSN"): same traffic from a pod
# WITHOUT a ResourceClaim -> no tx_flows entry -> everything untagged ---
hdr "TX baseline: claimless pod -> $ARM1_IP:4840/:9999 interleaved"
k apply -f ../../deployments/examples/vplc-baseline.yaml >/dev/null
k wait --for=condition=Ready pod/vplc-baseline -n "$NS" --timeout=120s >/dev/null \
  || { fail "vplc-baseline not Ready"; summary; exit 1; }
w1_capture tx_base.pcap out "(udp dst port 4840 or udp dst port 9999) or (vlan and (udp dst port 4840 or udp dst port 9999))"
sleep 3
k exec -n "$NS" vplc-baseline -- bash -c \
  "i=0; while [ \$i -lt $((FRAMES * 2)) ]; do p=4840; [ \$((i % 2)) = 1 ] && p=9999; printf 'DRATSN-TAG-%06d' \$i > /dev/udp/$ARM1_IP/\$p; i=\$((i+1)); sleep 0.01; done" \
  >/dev/null 2>&1
wait
fetch tx_base.pcap
BASE_POD_IP="$(jget '{.status.podIP}' pod vplc-baseline -n "$NS")"
k delete pod vplc-baseline -n "$NS" --force --grace-period=0 >/dev/null 2>&1

# --- with DRATSN from here: deploy the claimed fixture pod ---
deploy_fixture

# --- TX with DRATSN: concurrent OT(:4840) + IT(:9999), single egress capture ---
hdr "TX mix: pod -> $ARM1_IP:4840 (OT) interleaved with :9999 (IT)"
w1_capture tx_mix.pcap out "(udp dst port 4840 or udp dst port 9999) or (vlan and (udp dst port 4840 or udp dst port 9999))"
sleep 3; send_tx_mix; wait
fetch tx_mix.pcap

# --- RX: concurrent OT(talker $ARM1_IP) + IT(talker $IT_TALKER) injection ---
hdr "RX mix: inject vlan $RX_VLAN, talkers $ARM1_IP (OT) / $IT_TALKER (IT)"
w1_capture rx_wire.pcap in "(udp dst port $RX_ENDPOINT_PORT) or (vlan and udp dst port $RX_ENDPOINT_PORT)"
w1_xdpdump rx_xdp.pcap
pod_capture rx_pod.pcap
sleep 3
INJECT_START_NS=$(( $(now_ms) * 1000000 ))
# kernel TAI-REALTIME offset on the TSN node (0 unless NTP/PTP set it) — needed
# to pair rx_ts t3 (bpf_ktime_get_tai_ns) with pod pcap realtime stamps.
TAI_OFF_NS="$(node_ssh "$TSN_NODE_IP" "python3 -c 'import time; print(time.clock_gettime_ns(time.CLOCK_TAI) - time.clock_gettime_ns(time.CLOCK_REALTIME))'")"
inject_rx_mix; wait
k cp "$NS/mock-vplc:/tmp/rx_pod.pcap" "$OUT/rx_pod.pcap" >/dev/null 2>&1
fetch rx_wire.pcap; fetch rx_xdp.pcap
# t3 stamps (TAI ns) for the OT frames — pairs with the pod pcap (realtime) via
# the fixed TAI-UTC offset for XDP->pod latency (xdpdump's own clock is unusable:
# it is offset from realtime by an unknown constant).
node_sudo "$TSN_NODE_IP" "bpftool map dump pinned /sys/fs/bpf/tsn/rx_ts -j" > "$OUT/rx_ts.json"

node_sudo "$TSN_NODE_IP" "rm -rf $RTMP" >/dev/null
node_sudo "$INJECT_NODE_IP" "rm -rf $RTMP" >/dev/null

cat > "$OUT/meta.env" <<EOF
FRAMES=$FRAMES
POD_IP=$POD_IP
TX_VLAN=$TX_VLAN
TX_PCP=$TX_PCP
RX_VLAN=$RX_VLAN
RX_PCP=5
ARM1_IP=$ARM1_IP
RX_ENDPOINT_IP=$RX_ENDPOINT_IP
RX_ENDPOINT_PORT=$RX_ENDPOINT_PORT
IT_TALKER=$IT_TALKER
BASE_POD_IP=$BASE_POD_IP
INJECT_START_NS=$INJECT_START_NS
TAI_OFF_NS=$TAI_OFF_NS
EOF

ls -l "$OUT"
pass "captures collected -> $OUT ; next: ./parse.sh $OUT"
summary
