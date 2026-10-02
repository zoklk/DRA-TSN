#!/usr/bin/env bash
# One-command round-trip latency run, driven FROM m1 (kubectl + this repo):
#   1. exec mock_vplc in the vPLC pod  -> send.csv/recv.csv (t0/t4) in the pod
#   2. kubectl cp those out to OUT
#   3. dump the w1 eBPF tx_ts/rx_ts maps via `kubectl debug node` (m1 can't ssh
#      workers) -> tx_ts.csv (seq,t1,t2) / rx_ts.csv (seq,t3) in OUT
#   4. measure-stats.sh -> OUT/stats.txt  (per-stage p50/p99/max, all on w1 CLOCK_TAI)
# All four CSVs land in OUT; stats filters stale eBPF entries by run_start.
#
# Usage:
#   ./measure-roundtrip.sh OUT
#   OUT=scripts/results/busy-poll/arm1 ./measure-roundtrip.sh
#   ARMS="192.168.1.12,4840,4841,arm2" COUNT=10000 OUT=results/arm2 ./measure-roundtrip.sh
# Config (env, defaults in env.sh): ARMS COUNT RATE CPU POD NS TSN_NODE.
set -euo pipefail
. "$(dirname "$0")/env.sh"
have "$KUBECTL" || die "kubectl required"

OUT="${1:-${OUT:-$MEASURE_DIR}}"
ARMS="${ARMS:-192.168.1.11,4840,4840,arm1}"     # ip,txport,rxport,label (single = arm-1)
COUNT="${COUNT:-10000}"; RATE="${RATE:-500}"; CPU="${CPU:-2}"
POD="${POD:-mock-vplc}"; NODE="${TSN_NODE:-alpha-w1}"
BIN="${BIN:-/tmp/mock_vplc}"                     # binary path inside the pod
DBG_IMG="${DBG_IMG:-busybox:1.36}"
PODDIR="/tmp/mr.$$"
mkdir -p "$OUT"

info "1/4 run $BIN: ARMS=$ARMS COUNT=$COUNT RATE=$RATE CPU=$CPU  (pod $POD, ns $NS)"
$KUBECTL -n "$NS" exec "$POD" -- sh -c "rm -rf $PODDIR && mkdir -p $PODDIR"
$KUBECTL -n "$NS" exec "$POD" -- env ARMS="$ARMS" COUNT="$COUNT" RATE="$RATE" CPU="$CPU" \
  MEASURE_DIR="$PODDIR" "$BIN"

info "2/4 pull pod send/recv.csv -> $OUT"
$KUBECTL -n "$NS" cp "$POD:$PODDIR/send.csv" "$OUT/send.csv"
$KUBECTL -n "$NS" cp "$POD:$PODDIR/recv.csv" "$OUT/recv.csv"

info "3/4 dump eBPF tx_ts/rx_ts from $NODE (kubectl debug node)"
# parse runs ON the node (host python3+bpftool); only the small CSVs come back over stdout.
B64=$(base64 -w0 <<'PY'
import json,struct,subprocess
def dump(n): return json.loads(subprocess.run(["bpftool","map","dump","pinned","/sys/fs/bpf/tsn/"+n,"-j"],capture_output=True,text=True).stdout)
def ai(x): return int(x,16) if isinstance(x,str) else int(x)
def pk(k):
    if isinstance(k,list): return int.from_bytes(bytes(ai(b) for b in k),"little")
    if isinstance(k,dict): return ai(next(iter(k.values())))
    return ai(k)
def pv(v,f):
    nf=len(f)
    if isinstance(v,dict):
        if all(x in v for x in f): return [ai(v[x]) for x in f]
        if "value" in v: return pv(v["value"],f)
    if isinstance(v,list): return list(struct.unpack_from("<%dQ"%nf,bytes(ai(x) for x in v)))
    return None
def c(n,f,o,h):
    rows=[]
    for e in dump(n):
        ts=pv(e.get("value"),f)
        if ts and any(ts): rows.append(str(pk(e.get("key")))+","+",".join(map(str,ts)))
    open(o,"w").write(h+"\n"+"\n".join(rows)+"\n")
c("tx_ts",["t1","t2"],"/tmp/tx_ts.csv","seq,t1_ns,t2_ns")
c("rx_ts",["t3"],"/tmp/rx_ts.csv","seq,t3_ns")
PY
)
# `sleep N |` keeps the debug pod's stdin open so it doesn't exit before output flushes.
# Larger maps (e.g. 65536-entry TS arrays) need a longer hold or stdout truncates.
sleep "${DUMP_WAIT:-14}" | $KUBECTL debug "node/$NODE" -i --image="$DBG_IMG" --profile=sysadmin -q -- \
  chroot /host sh -c "echo $B64 | base64 -d | python3 -; echo ===TX===; cat /tmp/tx_ts.csv; echo ===RX===; cat /tmp/rx_ts.csv" 2>/dev/null \
  | awk '/===TX===/{t=1;next} /===RX===/{t=2;next} t==1{print > "'"$OUT"'/tx_ts.csv"} t==2{print > "'"$OUT"'/rx_ts.csv"}'
# clean up the node-debugger pod kubectl debug leaves behind
$KUBECTL get pod -A 2>/dev/null | awk '/node-debugger/{print $1,$2}' | \
  while read -r ns nm; do $KUBECTL delete pod -n "$ns" "$nm" --wait=false >/dev/null 2>&1 || true; done

[ -s "$OUT/tx_ts.csv" ] || warn "tx_ts.csv empty — data plane loaded? maps pinned on $NODE?"

info "4/4 stats -> $OUT/stats.txt"
MEASURE_DIR="$OUT" "$(dirname "$0")/measure-stats.sh" | tee "$OUT/stats.txt"
ok "done -> $OUT/{send,recv,tx_ts,rx_ts}.csv + stats.txt"
