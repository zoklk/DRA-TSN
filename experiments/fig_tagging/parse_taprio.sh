#!/usr/bin/env bash
# Parse taprio pcaps into per-packet cycle offsets.
#   ./parse_taprio.sh results/<RUN>
# taprio_packets.csv: side,class,cycle_idx,offset_us,tagged
#   side  = base (claimless) | mix (claimed)
#   class = ot (:4840) | it (:9999)
#   offset_us = (capture_epoch_ns - BASE_NS_realtime) mod CYCLE_NS, in µs.
#     BASE_NS is CLOCK_TAI; realtime = TAI - 37s and 37s % 10ms == 0, so the
#     TAI/realtime offset cancels in the mod (documented in run_taprio.sh).
# Prints a window-occupancy summary + verdict.
set -eu
OUT="${1:?usage: ./parse_taprio.sh results/<RUN>}"
OUT="$(cd "$OUT" && pwd)"
cd "$(dirname "$0")"
. "$OUT/taprio.env"
PKT="$OUT/taprio_packets.csv"
echo "side,class,cycle_idx,offset_us,tagged" > "$PKT"

for side in base mix; do
  pcap="$OUT/tx_taprio_$side.pcap"
  [ -s "$pcap" ] || continue
  tshark -r "$pcap" -Y "(udp.dstport==4840 || udp.dstport==9999) && ip.dst==$ARM1_IP" \
    -T fields -E separator='|' -e frame.time_epoch -e vlan.id -e udp.dstport 2>/dev/null \
  | python3 -c '
import sys
side, base, cyc = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
for line in sys.stdin:
    t, vlan, port = line.rstrip("\n").split("|")
    ns = int(float(t) * 1e9)
    rel = ns - (base - 37_000_000_000)   # TAI base -> realtime base
    cls = "ot" if port == "4840" else "it"
    print(f"{side},{cls},{rel // cyc},{(rel % cyc) / 1000:.1f},{1 if vlan else 0}")
' "$side" "$BASE_NS" "$CYCLE_NS" >> "$PKT"
done

rc=0
python3 - "$PKT" "$TSN_GATE_NS" <<'PY' || rc=$?
import sys, csv
pkt, gate_us = sys.argv[1], int(sys.argv[2]) / 1000
rows = list(csv.DictReader(open(pkt)))
print(f"{'side':6} {'class':5} {'total':>5} {'tsn_win':>7} {'be_win':>6} {'tagged':>6}")
bad = 0
for side in ("base", "mix"):
    for cls in ("ot", "it"):
        sel = [r for r in rows if r["side"] == side and r["class"] == cls]
        if not sel: continue
        tsn = sum(1 for r in sel if float(r["offset_us"]) < gate_us)
        tag = sum(1 for r in sel if r["tagged"] == "1")
        print(f"{side:6} {cls:5} {len(sel):5} {tsn:7} {len(sel)-tsn:6} {tag:6}")
        exp_tsn = len(sel) if (side == "mix" and cls == "ot") else 0
        if abs(tsn - exp_tsn) > len(sel) * 0.01:
            bad += 1
sys.exit(1 if bad else 0)
PY
[ "$rc" = 0 ] && echo "[PASS] window occupancy matches expectation (<1% boundary strays)" \
             || echo "[FAIL] window occupancy off expectation"
exit "$rc"
