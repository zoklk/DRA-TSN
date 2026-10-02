#!/usr/bin/env bash
# Join the round-trip measurement CSVs by seq and report per-stage latency.
# Inputs (in MEASURE_DIR): send.csv(seq,t0) recv.csv(seq,t4) tx_ts.csv(seq,t1,t2) rx_ts.csv(seq,t3).
# ALL timestamps are on the w1 clock (CLOCK_TAI) — no PTP. Reports p50/p99/max/min/count
# per stage (NOT mean — determinism = worst case, ADR-0024). Stale eBPF entries from a
# prior run are dropped (any eBPF ts < run start = leftover in the ARRAY map).
#
# Cycle stages (pod TX -> actuator -> echo -> pod RX):
#   app->veth    t0->t1   pod userspace -> host veth (TX)
#   veth->NICeg  t1->t2   eBPF tag+redirect (TX)
#   net+echo     t2->t3   wire out + actuator echo + wire back (round-trip, no PTP)
#   NICing->pod  t3->t4   eBPF DNAT+redirect -> pod socket (RX)
#   cycle        t0->t4   full round-trip = one control cycle
#
# TX-only (host-egress) extras — reported whenever the inputs allow, RX not needed:
#   HOST-EGRESS  t0->t2   pod userspace -> physical NIC egress (one-way, one clock)
#   tick-jitter  |Δt0 - period|   send-cadence deviation between consecutive seqs
#                (control-period jitter; period = PERIOD_NS env, default 1 ms)
#
# Usage: MEASURE_DIR=/path [PERIOD_NS=1000000] ./measure-stats.sh
set -uo pipefail
. "$(dirname "$0")/env.sh"
have python3 || die "python3 required"

DIR="$MEASURE_DIR" PERIOD_NS="${PERIOD_NS:-1000000}" python3 - <<'PY'
import os, csv
D=os.environ["DIR"]
def load(name, cols):
    p=os.path.join(D,name); d={}
    if not os.path.exists(p): return d
    with open(p) as f:
        for row in csv.DictReader(f):
            try: d[int(row["seq"])]={c:int(row[c]) for c in cols}
            except (KeyError,ValueError,TypeError): pass  # TypeError: truncated dump line (DictReader None)
    return d
send=load("send.csv",["t0_ns"]); recv=load("recv.csv",["t4_ns"])
txts=load("tx_ts.csv",["t1_ns","t2_ns"]); rxts=load("rx_ts.csv",["t3_ns"])
if not send: print("no send.csv — nothing to do"); raise SystemExit(0)
run_start=min(v["t0_ns"] for v in send.values())
def fresh(ts): return ts and ts>=run_start
def pct(xs,p):
    if not xs: return None
    xs=sorted(xs); return xs[min(len(xs)-1,int(round(p/100.0*(len(xs)-1))))]
def rep(name, xs):
    xs=[x for x in xs if x is not None and x>=0]
    if not xs: print(f"  {name:<20} (no samples)"); return
    us=lambda v: f"{v/1000:.2f}µs"
    print(f"  {name:<20} n={len(xs):<6} p50={us(pct(xs,50)):<11} p99={us(pct(xs,99)):<11} max={us(max(xs)):<11} min={us(min(xs))}")

print(f"run_start(TAI ns)={run_start}  sent={len(send)}  echoed-back={len(recv)}")
print("round-trip cycle (pod TX -> actuator echo -> pod RX), all w1 clock:")
rep("app->veth   t0t1", [txts[s]["t1_ns"]-send[s]["t0_ns"] for s in send if s in txts and fresh(txts[s]["t1_ns"])])
rep("veth->NICeg t1t2", [txts[s]["t2_ns"]-txts[s]["t1_ns"] for s in txts if fresh(txts[s]["t1_ns"]) and fresh(txts[s]["t2_ns"])])
rep("net+echo    t2t3", [rxts[s]["t3_ns"]-txts[s]["t2_ns"] for s in rxts if s in txts and fresh(txts[s]["t2_ns"]) and fresh(rxts[s]["t3_ns"])])
rep("NICing->pod t3t4", [recv[s]["t4_ns"]-rxts[s]["t3_ns"] for s in recv if s in rxts and fresh(rxts[s]["t3_ns"])])
rep("CYCLE       t0t4", [recv[s]["t4_ns"]-send[s]["t0_ns"] for s in recv if s in send])
print("host-egress path (pod TX -> NIC egress, no echo needed):")
rep("HOST-EGRESS t0t2", [txts[s]["t2_ns"]-send[s]["t0_ns"] for s in send if s in txts and fresh(txts[s]["t2_ns"])])
period=int(os.environ["PERIOD_NS"])
seqs=sorted(send)
rep("tick-jitter |Δt0-T|", [abs(send[s]["t0_ns"]-send[p]["t0_ns"]-period)
                            for p,s in zip(seqs,seqs[1:]) if s==p+1])
PY
