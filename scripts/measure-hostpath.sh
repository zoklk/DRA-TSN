#!/usr/bin/env bash
# One-command HOST-EGRESS latency run (TX-only, no actuator/echo needed), driven
# from m1. Measures pod userspace -> physical NIC egress (t0 -> t2) on one clock:
#   1. exec mock_vplc in the vPLC pod -> send.csv (t0 per seq); echoes may or may
#      not come back (recv.csv copied if present, stats tolerate its absence)
#   2. kubectl cp send/recv.csv out to OUT
#   3. dump the w1 eBPF tx_ts map over ssh (sudo bpftool, passwordless for bpftool
#      only) -> tx_ts.csv (seq,t1,t2). No kubectl-debug node hack.
#   4. snapshot w1 PSI (/proc/pressure/{memory,cpu}) before/after the run ->
#      psi_before.txt / psi_after.txt — the PSI signal is the Score-input proxy
#      candidate (context/dratsn-interference-scenario-design.md §2)
#   5. measure-stats.sh -> OUT/stats.txt (adds HOST-EGRESS t0t2 + tick-jitter)
#
# Usage:
#   W1_PASS=... ./measure-hostpath.sh OUT
#   W1_PASS=... COUNT=60000 RATE=500 OUT=results/interference/idle/rep1 ./measure-hostpath.sh
# Config (env, defaults in env.sh): ARMS COUNT RATE CPU POD NS BIN W1_HOST W1_USER W1_PASS.
set -euo pipefail
. "$(dirname "$0")/env.sh"
have "$KUBECTL" || have "${KUBECTL%% *}" || die "kubectl required"
have sshpass || die "sshpass required"

OUT="${1:-${OUT:-$MEASURE_DIR}}"
ARMS="${ARMS:-192.168.1.11,4840,4840,arm1}"     # ip,txport,rxport,label (single = arm-1)
COUNT="${COUNT:-60000}"; RATE="${RATE:-500}"; CPU="${CPU:-2}"
POD="${POD:-mock-vplc}"
BIN="${BIN:-/tmp/mock_vplc}"                     # binary path inside the pod
PODDIR="/tmp/mh.$$"
mkdir -p "$OUT"
[ "$COUNT" -le "$TS_MAX_SAMPLES" ] || die "COUNT $COUNT > TS_MAX_SAMPLES $TS_MAX_SAMPLES (seq wraps)"

info "0/4 PSI snapshot (before) from $W1_HOST"
w1ssh 'grep -H . /proc/pressure/memory /proc/pressure/cpu /proc/loadavg' > "$OUT/psi_before.txt"

info "1/4 run $BIN: ARMS=$ARMS COUNT=$COUNT RATE=$RATE CPU=$CPU  (pod $POD, ns $NS, TX-only)"
$KUBECTL -n "$NS" exec "$POD" -- sh -c "rm -rf $PODDIR && mkdir -p $PODDIR"
$KUBECTL -n "$NS" exec "$POD" -- env ARMS="$ARMS" COUNT="$COUNT" RATE="$RATE" CPU="$CPU" \
  GRACE="${GRACE:-1}" MEASURE_DIR="$PODDIR" "$BIN"

info "2/4 PSI snapshot (after) + pull pod send/recv.csv -> $OUT"
w1ssh 'grep -H . /proc/pressure/memory /proc/pressure/cpu /proc/loadavg' > "$OUT/psi_after.txt"
$KUBECTL -n "$NS" cp "$POD:$PODDIR/send.csv" "$OUT/send.csv"
$KUBECTL -n "$NS" cp "$POD:$PODDIR/recv.csv" "$OUT/recv.csv" 2>/dev/null || true

info "3/4 dump eBPF tx_ts from $W1_HOST (ssh + sudo bpftool)"
w1ssh "sudo -n /usr/sbin/bpftool map dump pinned $PIN_DIR/tx_ts -j" > "$OUT/tx_ts.json"
python3 - "$OUT" <<'PY'
import json,struct,sys,os
out=sys.argv[1]
def ai(x): return int(x,16) if isinstance(x,str) else int(x)
def pk(k):
    if isinstance(k,list): return int.from_bytes(bytes(ai(b) for b in k),"little")
    if isinstance(k,dict): return ai(next(iter(k.values())))
    return ai(k)
def pv(v,f):
    if isinstance(v,dict):
        if all(x in v for x in f): return [ai(v[x]) for x in f]
        if "value" in v: return pv(v["value"],f)
    if isinstance(v,list): return list(struct.unpack_from("<%dQ"%len(f),bytes(ai(x) for x in v)))
    return None
rows=[]
for e in json.load(open(os.path.join(out,"tx_ts.json"))):
    ts=pv(e.get("value"),["t1","t2"])
    if ts and any(ts): rows.append(f'{pk(e.get("key"))},{ts[0]},{ts[1]}')
open(os.path.join(out,"tx_ts.csv"),"w").write("seq,t1_ns,t2_ns\n"+"\n".join(rows)+"\n")
PY
rm -f "$OUT/tx_ts.json"
[ -s "$OUT/tx_ts.csv" ] || warn "tx_ts.csv empty — data plane loaded? tx_flows entry for the probe dst?"

info "4/4 stats -> $OUT/stats.txt"
MEASURE_DIR="$OUT" PERIOD_NS=$((1000000000 / RATE)) "$(dirname "$0")/measure-stats.sh" | tee "$OUT/stats.txt"
ok "done -> $OUT/{send,tx_ts}.csv + psi_{before,after}.txt + stats.txt"
