#!/usr/bin/env bash
# Parse fig_tagging pcaps with tshark.
#   ./parse.sh results/<RUN>
# Produces:
#   summary.csv  scenario,point,total,tagged_correct,tagged_wrong,untagged,delivered
#   packets.csv  side,point,class,t_rel,vlan,pcp,ip_dst   (one row per frame;
#                t_rel = seconds from the first probe frame in that pcap)
# Classification: TX by dst port (4840=ot / 9999=it), RX by talker IP.
set -eu
OUT="${1:?usage: ./parse.sh results/<RUN>}"
OUT="$(cd "$OUT" && pwd)"
cd "$(dirname "$0")"
. "$OUT/meta.env"
SUM="$OUT/summary.csv"; PKT="$OUT/packets.csv"
echo "scenario,point,total,tagged_correct,tagged_wrong,untagged,delivered" > "$SUM"
echo "side,point,class,t_rel,vlan,pcp,ip_dst" > "$PKT"

# fields <pcap> <display filter> -> time|vlan|prio|src|dst|dstport
fields() {
  tshark -r "$1" -Y "$2" -T fields -E separator='|' \
    -e frame.time_epoch -e vlan.id -e vlan.priority -e ip.src -e ip.dst -e udp.dstport 2>/dev/null
}

# emit <side> <point> <pcap> <filter> <class_mode>
#   class_mode: port  (4840=ot, else it) | talker (ARM1_IP=ot, else it)
# appends packets.csv rows and prints "class|counts" aggregation lines
emit() {
  local side="$1" point="$2" pcap="$3" filt="$4" mode="$5"
  [ -s "$pcap" ] || return 0
  fields "$pcap" "$filt" | sort -t'|' -k1,1n | awk -F'|' \
    -v side="$side" -v point="$point" -v mode="$mode" -v ot_talker="$ARM1_IP" -v pkt="$PKT" '
    NR==1 { t0=$1 }
    { cls = "it"
      if (mode=="port"   && $6==4840)      cls="ot"
      if (mode=="talker" && $4==ot_talker) cls="ot"
      printf "%s,%s,%s,%.4f,%s,%s,%s\n", side, point, cls, $1-t0, $2, $3, $5 >> pkt
    }'
}

# count <side_class> <point> <pcap> <filter> <expect_vlan> <expect_pcp> -> summary row
count() {
  local scen="$1" point="$2" pcap="$3" filt="$4" evlan="$5" epcp="$6"
  if [ ! -s "$pcap" ]; then echo "$scen,$point,0,0,0,0,0" >> "$SUM"; return; fi
  local c
  c="$(fields "$pcap" "$filt" | awk -F'|' -v ev="$evlan" -v ep="$epcp" -v pod="$POD_IP" '
    { total++
      if ($2 == "") { untag++ }
      else if ($2 == ev && $3 == ep) { ok++ }
      else { wrong++ }
      if ($5 == pod) { deliv++ }
    }
    END { printf "%d,%d,%d,%d,%d\n", total+0, ok+0, wrong+0, untag+0, deliv+0 }')"
  echo "$scen,$point,$c" >> "$SUM"
}

TXP="$OUT/tx_mix.pcap"; TXB="$OUT/tx_base.pcap"
RXW="$OUT/rx_wire.pcap"; RXX="$OUT/rx_xdp.pcap"; RXP="$OUT/rx_pod.pcap"
PROBE_RX="udp.dstport==$RX_ENDPOINT_PORT && (ip.src==$ARM1_IP || ip.src==$IT_TALKER)"

# summary (2x2 view, same semantics as before; txbase-* = claimless ablation)
count tx-ot wire "$TXP" "udp.dstport==4840 && ip.dst==$ARM1_IP" "$TX_VLAN" "$TX_PCP"
count tx-it wire "$TXP" "udp.dstport==9999 && ip.dst==$ARM1_IP" "$TX_VLAN" "$TX_PCP"
count txbase-ot wire "$TXB" "udp.dstport==4840 && ip.dst==$ARM1_IP" "$TX_VLAN" "$TX_PCP"
count txbase-it wire "$TXB" "udp.dstport==9999 && ip.dst==$ARM1_IP" "$TX_VLAN" "$TX_PCP"
for pt in wire:$RXW xdp:$RXX pod:$RXP; do
  count rx-ot "${pt%%:*}" "${pt#*:}" "udp.dstport==$RX_ENDPOINT_PORT && ip.src==$ARM1_IP"  "$RX_VLAN" "$RX_PCP"
  count rx-it "${pt%%:*}" "${pt#*:}" "udp.dstport==$RX_ENDPOINT_PORT && ip.src==$IT_TALKER" "$RX_VLAN" "$RX_PCP"
done
for pt in wire:$OUT/rxbase_wire.pcap xdp:$OUT/rxbase_xdp.pcap pod:$OUT/rxbase_pod.pcap; do
  count rxbase-ot "${pt%%:*}" "${pt#*:}" "udp.dstport==$RX_ENDPOINT_PORT && ip.src==$ARM1_IP"  "$RX_VLAN" "$RX_PCP"
  count rxbase-it "${pt%%:*}" "${pt#*:}" "udp.dstport==$RX_ENDPOINT_PORT && ip.src==$IT_TALKER" "$RX_VLAN" "$RX_PCP"
done

# per-packet rows
emit tx wire "$TXP" "(udp.dstport==4840 || udp.dstport==9999) && ip.dst==$ARM1_IP" port
emit txbase wire "$TXB" "(udp.dstport==4840 || udp.dstport==9999) && ip.dst==$ARM1_IP" port
emit rx xdp  "$RXX" "$PROBE_RX" talker
emit rx wire "$RXW" "$PROBE_RX" talker
emit rx pod  "$RXP" "$PROBE_RX" talker
emit rxbase xdp  "$OUT/rxbase_xdp.pcap"  "$PROBE_RX" talker
emit rxbase wire "$OUT/rxbase_wire.pcap" "$PROBE_RX" talker

# --- rx_latency.csv: per injected frame, XDP(t3, TAI from rx_ts) -> pod arrival
# (pod pcap realtime + 37s = TAI). Frames without both stamps = not delivered.
if [ -s "$OUT/rx_ts.json" ]; then
  LAT="$OUT/rx_latency.csv"
  FR="$(mktemp)"
  {
    tshark -r "$RXP" -Y "$PROBE_RX" -T fields -E separator='|' \
      -e frame.time_epoch -e ip.src -e data.data 2>/dev/null | sed 's/^/pod|/'
    tshark -r "$RXX" -Y "$PROBE_RX" -T fields -E separator='|' \
      -e frame.time_epoch -e ip.src -e data.data 2>/dev/null | sed 's/^/xdp|/'
    tshark -r "$OUT/rxbase_xdp.pcap" -Y "$PROBE_RX" -T fields -E separator='|' \
      -e frame.time_epoch -e ip.src -e data.data 2>/dev/null | sed 's/^/xdpb|/'
  } > "$FR"
  python3 - "$OUT/rx_ts.json" "$ARM1_IP" "${INJECT_START_NS:-0}" "${TAI_OFF_NS:-0}" "$FR" > "$LAT" <<'PY'
import sys, json

ts_json, ot_ip, start_ns = sys.argv[1], sys.argv[2], int(sys.argv[3])
# kernel CLOCK_TAI - CLOCK_REALTIME on the TSN node, measured by run.sh
# (0 on stock Ubuntu: the kernel tai offset is unset unless NTP/PTP programs it)
TAI_OFF = int(sys.argv[4])

def payload_seq(hexstr):
    h = hexstr.replace(":", "")
    if len(h) < 16 or h[:8].lower() != "54534e31":
        return None
    return int(h[8:16], 16)

# t3 (TAI ns) per seq from the rx_ts array map; drop stale slots from earlier
# runs. bpftool -j wraps BTF output as {"key":[bytes],"value":[bytes],
# "formatted":{"key":N,"value":{"t3":N}}} — prefer the formatted view.
t3 = {}
for e in json.load(open(ts_json)):
    f = e.get("formatted", e)
    k, v = f.get("key"), f.get("value", {})
    if isinstance(k, list):
        k = int.from_bytes(bytes(int(b, 0) for b in k), "little")
    elif isinstance(k, str):
        k = int(k, 0)
    t = v.get("t3") if isinstance(v, dict) else None
    if isinstance(t, str):
        t = int(t, 0)
    if t and (start_ns == 0 or t >= start_ns + TAI_OFF - 2_000_000_000):
        t3[int(k)] = t

pod, seen = {}, {"rx": {}, "rxbase": {}}
for line in open(sys.argv[5]):
    where, t, src, data = line.rstrip("\n").split("|")
    seq = payload_seq(data)
    if seq is None:
        continue
    cls = "ot" if src == ot_ip else "it"
    if where == "pod":
        pod[seq] = int(float(t) * 1e9) + TAI_OFF
    elif where == "xdp":
        seen["rx"][seq] = cls
    else:
        seen["rxbase"][seq] = cls

print("side,seq,class,lat_us,delivered")
for side in ("rxbase", "rx"):
    for seq in sorted(seen[side]):
        cls = seen[side][seq]
        if side == "rx" and seq in pod and seq in t3:
            print(f"{side},{seq},{cls},{(pod[seq] - t3[seq]) / 1000:.2f},1")
        else:
            print(f"{side},{seq},{cls},,0")
PY
  rm -f "$FR"
  d=$(awk -F, 'NR>1 && $5==1{c++} END{print c+0}' "$LAT")
  echo "rx_latency.csv: $(($(wc -l < "$LAT") - 1)) frames, $d delivered with t3->pod latency"
fi

column -t -s, "$SUM"
echo; echo "packets.csv: $(($(wc -l < "$PKT") - 1)) rows"
