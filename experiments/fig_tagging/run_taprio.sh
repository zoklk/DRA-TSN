#!/usr/bin/env bash
# fig_tagging taprio scenario: show that DRATSN's per-flow skb->priority
# (ADR-0008) actually lands OT frames in their taprio gate window — the
# per-packet cycle scatter of arXiv:2503.12878 Fig.2, on our data plane.
#
# taprio (software mode) on TSN_IFACE, cycle 10ms:
#   tc1 (prio TX_PCP) gate open [0, 2.5ms)   -- "TSN window"
#   tc0 (best effort)  gate open [2.5, 10ms) -- "BE window"
# Frames are paced at 13ms (gcd(13,10)=1) so send phases sweep the cycle.
#
#   (a) tx_taprio_base.pcap: claimless pod  -> prio 0 -> everything BE window
#   (b) tx_taprio_mix.pcap : claimed pod    -> OT in TSN window, IT in BE window
#
# Capture on eno1 -Q out: tcpdump taps after qdisc dequeue, so timestamps are
# gate-release times. base-time is a 10ms multiple in CLOCK_TAI; TAI-UTC (37s)
# is also a 10ms multiple, so mod-cycle math on realtime stamps is exact.
#
# Output: results/<RUN>/tx_taprio_{base,mix}.pcap + taprio.env
# Then:   ./parse_taprio.sh results/<RUN>  ->  taprio_packets.csv
# Usage:  FRAMES=200 RUN=myrun ./run_taprio.sh
set -u
cd "$(dirname "$0")"
. ../lib.sh
PHASE="fig_tagging_taprio"
: "${RUN:=$(date +%Y%m%d-%H%M%S)}"
: "${CYCLE_NS:=10000000}"        # 10ms
: "${TSN_GATE_NS:=2500000}"      # tc1 open 2.5ms
: "${PACE_S:=0.013}"             # 13ms send pace (never aligns with the cycle)
OUT="results/$RUN"; mkdir -p "$OUT"
RTMP=/tmp/exp-tagging
CAP_SECS=$(( (FRAMES * 2 * 13 / 1000) + 12 ))

hdr "fig_tagging taprio (FRAMES=$FRAMES/class, cycle ${CYCLE_NS}ns -> $OUT)"
require kubectl || { summary; exit 1; }
ensure_ns
cnc_pf_start; ensure_streams; cnc_pf_stop
node_ssh "$TSN_NODE_IP" "mkdir -p $RTMP"

teardown() {
  node_sudo "$TSN_NODE_IP" "tc qdisc del dev $TSN_IFACE root 2>/dev/null; tc qdisc show dev $TSN_IFACE | head -1"
}
trap 'info "restoring default qdisc"; teardown' EXIT

# --- taprio setup: base-time = (TAI now + 3s) floored to a cycle boundary ---
BASE_NS="$(node_ssh "$TSN_NODE_IP" "python3 -c 'import time; t=time.clock_gettime_ns(time.CLOCK_TAI)+3_000_000_000; print(t - t % $CYCLE_NS)'")"
BE_NS=$((CYCLE_NS - TSN_GATE_NS))
info "taprio base-time=$BASE_NS (TAI), gates: tc1 ${TSN_GATE_NS}ns / tc0 ${BE_NS}ns"
node_sudo "$TSN_NODE_IP" "tc qdisc replace dev $TSN_IFACE root taprio num_tc 2 \
  map 0 0 0 0 0 0 1 0 0 0 0 0 0 0 0 0 \
  queues 3@0 1@3 base-time $BASE_NS clockid CLOCK_TAI \
  sched-entry S 2 $TSN_GATE_NS sched-entry S 1 $BE_NS flags 0x0" \
  || { fail "taprio setup failed"; summary; exit 1; }
node_sudo "$TSN_NODE_IP" "tc qdisc show dev $TSN_IFACE | head -2"

w1_capture() { # <file>
  node_sudo "$TSN_NODE_IP" \
    "timeout $CAP_SECS tcpdump -i $TSN_IFACE -Q out -nn -w $RTMP/$1 \
     '(udp dst port 4840 or udp dst port 9999) or (vlan and (udp dst port 4840 or udp dst port 9999))' \
     >/dev/null 2>&1; chmod 644 $RTMP/$1" &
}
send_mix() { # <pod>
  k exec -n "$NS" "$1" -- bash -c \
    "i=0; while [ \$i -lt $((FRAMES * 2)) ]; do p=4840; [ \$((i % 2)) = 1 ] && p=9999; printf 'DRATSN-TAG-%06d' \$i > /dev/udp/$ARM1_IP/\$p; i=\$((i+1)); sleep $PACE_S; done" \
    >/dev/null 2>&1
}
fetch() { node_scp "$SSH_USER@$TSN_NODE_IP:$RTMP/$1" "$OUT/$1"; }

# --- (a) baseline: claimless pod, prio 0 -> BE window only ---
hdr "(a) baseline pod under taprio"
if resource_exists pod mock-vplc -n "$NS"; then
  k delete pod mock-vplc -n "$NS" --wait=true >/dev/null
  wait_for 60 claims_gone
fi
k apply -f ../../deployments/examples/vplc-baseline.yaml >/dev/null
k wait --for=condition=Ready pod/vplc-baseline -n "$NS" --timeout=120s >/dev/null \
  || { fail "vplc-baseline not Ready"; summary; exit 1; }
w1_capture tx_taprio_base.pcap
sleep 3; send_mix vplc-baseline; wait
fetch tx_taprio_base.pcap
k delete pod vplc-baseline -n "$NS" --force --grace-period=0 >/dev/null 2>&1

# --- (b) DRATSN: claimed pod, OT prio TX_PCP -> TSN window ---
hdr "(b) claimed pod under taprio"
k apply -f ../../deployments/examples/mock-vplc.yaml >/dev/null
k wait --for=condition=Ready pod/mock-vplc -n "$NS" --timeout=120s >/dev/null \
  || { fail "mock-vplc not Ready"; summary; exit 1; }
sleep 3
w1_capture tx_taprio_mix.pcap
sleep 3; send_mix mock-vplc; wait
fetch tx_taprio_mix.pcap

node_sudo "$TSN_NODE_IP" "rm -rf $RTMP" >/dev/null

cat > "$OUT/taprio.env" <<EOF
FRAMES=$FRAMES
BASE_NS=$BASE_NS
CYCLE_NS=$CYCLE_NS
TSN_GATE_NS=$TSN_GATE_NS
TX_VLAN=$TX_VLAN
TX_PCP=$TX_PCP
ARM1_IP=$ARM1_IP
EOF

pass "taprio captures collected -> $OUT ; next: ./parse_taprio.sh $OUT"
summary
