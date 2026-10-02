#!/usr/bin/env bash
# DRATSN data-plane END-TO-END verification (actual packet transforms, not just
# "map programmed"). Run ON the worker node that hosts the vplc pod, as root.
#
#   sudo NS=dratsn-test TSN_IFACE=eno1 RX_ENDPOINT_IP=192.168.1.1 \
#        ./tests/dataplane_e2e.sh all
#
# Modes (tx/rx/all run ON the vPLC node; inject-rx/capture-tx run ON a peer node):
#   tx          live TX: pod sends UDP -> capture on the TSN NIC -> assert VLAN/PCP
#               tag, L2 rebuild (src=NIC, dst=listener), src IP (SNAT on/off).
#   rx          RX transform via BPF_PROG_TEST_RUN: feed a VLAN-tagged UDP frame to
#               the loaded tsn_rx program; assert XDP_REDIRECT + tag stripped + dst
#               DNATed to the pod. (Deterministic, needs no TSN switch.)
#   all         tx + rx (default).
#   inject-rx   [peer node] send a real tagged UDP frame to the vPLC node's TSN NIC
#               endpoint (cross-node RX wire test). Needs PEER_IFACE + TSN_NIC_MAC.
#   capture-tx  [peer node] tcpdump the vPLC node's tagged TX frames arriving here
#               (cross-node TX wire test; CNC TX listener must point at this peer).
#
# Why RX uses prog_test_run: a single host can't inject a frame into its own NIC's
# XDP ingress (locally-generated TX doesn't traverse RX). prog_test_run exercises
# the exact strip+DNAT logic with a crafted packet. For a true on-wire RX test the
# script prints a ready-to-run scapy line for an EXTERNAL sender on the TSN segment.
set -uo pipefail
cd "$(dirname "$0")"
# lib.sh provides pass/fail/skip/info/hdr/require/summary. Peer nodes often have
# only this script copied over (no lib.sh) — fall back to minimal stubs so the
# inject-rx / capture-tx modes still run standalone.
if [ -f ./lib.sh ]; then
  . ./lib.sh
else
  FAIL=0
  pass()    { echo "[PASS] $*"; }
  fail()    { echo "[FAIL] $*"; FAIL=$((FAIL+1)); }
  skip()    { echo "[SKIP] $*"; }
  info()    { echo "[ .. ] $*"; }
  hdr()     { printf '\n=== %s ===\n' "$*"; }
  require() { command -v "$1" >/dev/null 2>&1 || { skip "tool '$1' missing"; return 1; }; }
  summary() { [ "${FAIL:-0}" -eq 0 ]; }
fi
PHASE="dataplane-e2e"

: "${TSN_IFACE:=eno1}"
: "${PIN:=/sys/fs/bpf/tsn}"
: "${POD_LABEL:=app=vplc}"
# Expected TX stream params (defaults match tests/loop.sh scenario s-tx = arm-1 .11).
: "${TX_DST_IP:=192.168.1.11}"
: "${TX_DST_PORT:=4840}"
: "${TX_DST_MAC:=0c:c4:7a:ca:3b:f2}"
: "${TX_VLAN:=200}"
: "${TX_PCP:=6}"
# If TX SNAT is enabled (daemonset env TSN_SNAT_SOURCE_IP=<nodeIP>), set this to
# that IP so the wire src-IP assertion expects it; leave empty for pass-through.
: "${SNAT_SRC_IP:=}"
# RX endpoint (the node TSN NIC host IP:port = rx_flows key) + tagged vlan to send.
: "${RX_ENDPOINT_PORT:=4840}"
: "${RX_VLAN:=100}"
# RX talker (sensor) src IP — part of the rx_flows key (ADR-0027 stream-id
# hardening). Must match the s-rx stream's talker.ip or the lookup misses (arm-1 .11).
: "${RX_TALKER_IP:=192.168.1.11}"
# Peer-node (cross-node wire test) config — used by inject-rx / capture-tx, which
# run ON a peer worker on the same physical TSN L2 segment as the vPLC node.
: "${PEER_IFACE:=}"          # peer's NIC on the TSN segment (e.g. eno1 on the external sensor node)
: "${TSN_NIC_MAC:=}"         # the vPLC node's TSN NIC MAC (RX frame dst); kubectl can fetch it

hdr "DRATSN data-plane E2E (iface=$TSN_IFACE)"

nic_mac() { ip -o link show "$1" 2>/dev/null | grep -o 'link/ether [0-9a-f:]*' | awk '{print $2}'; }
first_pod() { k get pods -n "$NS" -l "$POD_LABEL" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null; }
pod_ip() { k get pod -n "$NS" "$1" -o jsonpath='{.status.podIP}' 2>/dev/null; }

# ---------------------------------------------------------------------------
tx_test() {
  hdr "TX — live capture on $TSN_IFACE"
  require tcpdump || return 0
  require kubectl || return 0
  local nicmac pod pip
  nicmac="$(nic_mac "$TSN_IFACE")"
  [ -n "$nicmac" ] || { fail "TX: cannot read MAC of $TSN_IFACE"; return 0; }
  pod="$(first_pod)"; [ -n "$pod" ] || { skip "TX: no pod with $POD_LABEL in $NS"; return 0; }
  pip="$(pod_ip "$pod")"
  info "TX: pod=$pod podIP=$pip nic=$TSN_IFACE/$nicmac -> dst $TX_DST_IP:$TX_DST_PORT"

  local cap; cap="$(mktemp)"
  timeout 9 tcpdump -i "$TSN_IFACE" -e -nn -c 3 -l \
    "udp and dst host $TX_DST_IP and dst port $TX_DST_PORT" >"$cap" 2>/dev/null &
  local tpid=$!
  sleep 1
  # generate the TSN TX flow from inside the pod (busybox nc).
  for _ in 1 2 3 4 5; do
    k exec -n "$NS" "$pod" -- sh -c "echo -n DRATSN-TX | nc -u -w1 $TX_DST_IP $TX_DST_PORT" >/dev/null 2>&1
    sleep 0.5
  done
  wait "$tpid" 2>/dev/null
  local line; line="$(grep -m1 "$TX_DST_IP\.$TX_DST_PORT" "$cap")"
  if [ -z "$line" ]; then
    fail "TX: no tagged frame captured on $TSN_IFACE (redirect/tagging not working?)"
    info "  (hint: confirm tsn_tx TCX on the pod host-veth and tx_flows entry)"
    rm -f "$cap"; return 0
  fi
  info "TX: captured: $line"

  # VLAN + PCP
  if echo "$line" | grep -qE "vlan $TX_VLAN,"; then pass "TX: 802.1Q VLAN $TX_VLAN present"
  else fail "TX: expected vlan $TX_VLAN not in frame"; fi
  if echo "$line" | grep -qE "vlan $TX_VLAN, p $TX_PCP"; then pass "TX: PCP $TX_PCP present"
  else fail "TX: expected pcp $TX_PCP not in frame"; fi
  # L2: src=NIC mac, dst=listener mac
  if echo "$line" | grep -qiE "$nicmac > $TX_DST_MAC"; then
    pass "TX: L2 rebuilt (src=NIC $nicmac, dst=listener $TX_DST_MAC)"
  else fail "TX: L2 not rebuilt as src=$nicmac dst=$TX_DST_MAC"; fi
  # src IP: SNAT on -> node IP, off -> pod IP
  local srcip; srcip="$(echo "$line" | grep -oE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+\.'"$TX_DST_PORT"' >' | head -1)"
  srcip="${srcip%.*}"; srcip="${srcip% >}"
  if [ -n "$SNAT_SRC_IP" ]; then
    if [ "$srcip" = "$SNAT_SRC_IP" ]; then pass "TX: SNAT on, src IP=$srcip (node)"
    else fail "TX: SNAT expected src=$SNAT_SRC_IP but wire src=$srcip"; fi
  else
    if [ "$srcip" = "$pip" ]; then pass "TX: SNAT off, src IP=$pip (pod, pass-through)"
    else info "TX: wire src IP=$srcip (pod=$pip; set SNAT_SRC_IP if SNAT enabled)"; fi
  fi
  rm -f "$cap"
}

# ---------------------------------------------------------------------------
rx_test() {
  hdr "RX — strip+DNAT transform via BPF_PROG_TEST_RUN"
  require bpftool || return 0
  require python3 || return 0
  require kubectl || return 0
  [ -n "$RX_ENDPOINT_IP" ] || { skip "RX: set RX_ENDPOINT_IP (node TSN NIC host IP)"; return 0; }

  local progid pod pip nicmac
  progid="$(bpftool -j prog show name tsn_rx 2>/dev/null | python3 -c 'import sys,json
try: d=json.load(sys.stdin)
except: sys.exit()
d=d if isinstance(d,list) else [d]
print(d[0]["id"]) if d else None' 2>/dev/null)"
  [ -n "$progid" ] || { skip "RX: tsn_rx not loaded on this node (run on the worker)"; return 0; }
  pod="$(first_pod)"; [ -n "$pod" ] || { skip "RX: no pod with $POD_LABEL in $NS"; return 0; }
  pip="$(pod_ip "$pod")"; [ -n "$pip" ] || { skip "RX: pod $pod has no IP yet"; return 0; }
  nicmac="$(nic_mac "$TSN_IFACE")"
  info "RX: prog id=$progid, endpoint=$RX_ENDPOINT_IP:$RX_ENDPOINT_PORT vlan=$RX_VLAN -> pod $pod ($pip:$RX_ENDPOINT_PORT)"

  local din dout; din="$(mktemp)"; dout="$(mktemp)"
  # Build a VLAN-tagged UDP frame to the node RX endpoint (csum=0; the program
  # uses incremental csum update and we assert on addresses/ethertype, not csum).
  # src IP = talker (part of the rx_flows key, ADR-0027); src/vlan are overridable
  # so the negative cases below can flip them to prove the hardening drops them.
  build_rx_frame() { # <outfile> <src_ip> <vlan>
    python3 - "$1" "$nicmac" "$3" "$RX_ENDPOINT_IP" "$RX_ENDPOINT_PORT" "$2" <<'PY'
import sys,struct,socket
out,dmac,vlan,dip,dport,sip=sys.argv[1],sys.argv[2],int(sys.argv[3]),sys.argv[4],int(sys.argv[5]),sys.argv[6]
def mac(s): return bytes(int(x,16) for x in s.split(':'))
eth=mac(dmac)+mac("02:00:00:00:99:99")+struct.pack("!H",0x8100)
dot1q=struct.pack("!H",(6<<13)|(vlan&0xfff))+struct.pack("!H",0x0800)
pay=b"DRATSN-RX"
udp=struct.pack("!HHHH",12345,dport,8+len(pay),0)+pay
iplen=20+len(udp)
ip=struct.pack("!BBHHHBBH4s4s",0x45,0,iplen,1,0,64,17,0,
               socket.inet_aton(sip),socket.inet_aton(dip))
open(out,"wb").write(eth+dot1q+ip+udp)
PY
  }
  rx_run_ret() { # <infile> -> echoes return value
    local r; r="$(bpftool prog run id "$progid" data_in "$1" data_out "$dout" repeat 1 2>&1)"
    echo "$r" | grep -oiE 'return value: *[0-9]+' | grep -oE '[0-9]+' | head -1
  }

  build_rx_frame "$din" "$RX_TALKER_IP" "$RX_VLAN"
  local ret; ret="$(rx_run_ret "$din")"
  info "RX: prog run (talker=$RX_TALKER_IP vlan=$RX_VLAN) -> return $ret"
  # XDP_REDIRECT = 4
  if [ "$ret" = "4" ]; then pass "RX: tsn_rx returned XDP_REDIRECT (lookup hit + redirect)"
  elif [ "$ret" = "2" ]; then fail "RX: returned XDP_PASS (rx_flows miss — RX stream claimed? talker IP=$RX_TALKER_IP matches s-rx?)"; rm -f "$din" "$dout"; return 0
  else fail "RX: unexpected return value '$ret'"; rm -f "$din" "$dout"; return 0; fi

  # Negative (ADR-0027): a frame from a spoofed src or wrong VID to the same
  # endpoint must NOT be DNATed — tsn_rx returns XDP_PASS (2) -> cilium normal.
  local nin; nin="$(mktemp)"
  build_rx_frame "$nin" "10.0.0.99" "$RX_VLAN"   # wrong src (key miss)
  local nret; nret="$(rx_run_ret "$nin")"
  if [ "$nret" = "2" ]; then pass "RX: spoofed src 10.0.0.99 -> XDP_PASS (not DNATed)"
  else fail "RX: spoofed src should XDP_PASS(2) but got '$nret' (src not in key?)"; fi
  build_rx_frame "$nin" "$RX_TALKER_IP" "$((RX_VLAN+1))"  # right src, wrong VID
  nret="$(rx_run_ret "$nin")"
  if [ "$nret" = "2" ]; then pass "RX: wrong VID $((RX_VLAN+1)) -> XDP_PASS (VID enforced)"
  else fail "RX: wrong VID should XDP_PASS(2) but got '$nret' (expected_vid enforced?)"; fi
  rm -f "$nin"

  # Parse data_out: vlan stripped (ethertype 0x0800 at offset 12) + dst DNATed.
  python3 - "$dout" "$pip" "$RX_ENDPOINT_PORT" <<'PY' && pass "RX: VLAN stripped + DNAT dst=$pip:$RX_ENDPOINT_PORT" || fail "RX: data_out transform mismatch"
import sys,struct,socket
data=open(sys.argv[1],"rb").read(); pip=sys.argv[2]; dport=int(sys.argv[3])
ok=True
eth_type=struct.unpack_from("!H",data,12)[0]
if eth_type!=0x0800: print("  ethertype not IPv4 (vlan not stripped): 0x%04x"%eth_type); ok=False
ip_dst=socket.inet_ntoa(data[30:34])           # eth14 + ip dst offset16
udp_dport=struct.unpack_from("!H",data,36)[0]  # eth14 + ip20 + udp dst offset2
if ip_dst!=pip: print("  IP dst %s != pod %s"%(ip_dst,pip)); ok=False
if udp_dport!=dport: print("  UDP dport %d != %d"%(udp_dport,dport)); ok=False
sys.exit(0 if ok else 1)
PY
  rm -f "$din" "$dout"

  echo
  info "RX live (real wire, optional): from an EXTERNAL host on the TSN segment run:"
  info "  python3 -c 'from scapy.all import *; sendp(Ether(dst=\"$nicmac\")/Dot1Q(vlan=$RX_VLAN,prio=5)/IP(src=\"$RX_TALKER_IP\",dst=\"$RX_ENDPOINT_IP\")/UDP(dport=$RX_ENDPOINT_PORT)/b\"X\", iface=\"<sender-if>\")'"
  info "  (src MUST be the s-rx talker $RX_TALKER_IP and vlan=$RX_VLAN — else tsn_rx XDP_PASSes it, ADR-0027)"
  info "  then in the pod: tcpdump -i eth0 -nn udp  (expect untagged, dst $pip:$RX_ENDPOINT_PORT)"
}

# ---------------------------------------------------------------------------
# Cross-node modes — run ON a peer worker on the same TSN L2 segment, as root.

# inject-rx: send a real VLAN-tagged UDP frame out PEER_IFACE to the vPLC node's
# TSN NIC endpoint, so the vPLC node's XDP RX strips+DNATs it into the pod.
inject_rx() {
  hdr "inject-rx — send tagged UDP to $RX_ENDPOINT_IP:$RX_ENDPOINT_PORT via $PEER_IFACE"
  require python3 || return 0
  [ -n "$PEER_IFACE" ]    || { fail "set PEER_IFACE (peer NIC on the TSN segment)"; return 0; }
  [ -n "$RX_ENDPOINT_IP" ] || { fail "set RX_ENDPOINT_IP (vPLC node TSN NIC IP)"; return 0; }
  [ -n "$TSN_NIC_MAC" ]   || { fail "set TSN_NIC_MAC (vPLC node TSN NIC MAC = frame dst)"; return 0; }
  python3 - "$PEER_IFACE" "$TSN_NIC_MAC" "$RX_VLAN" "$RX_ENDPOINT_IP" "$RX_ENDPOINT_PORT" "$RX_TALKER_IP" <<'PY'
import sys,struct,socket
iface,dmac,vlan,dip,dport,sip=sys.argv[1],sys.argv[2],int(sys.argv[3]),sys.argv[4],int(sys.argv[5]),sys.argv[6]
mac=lambda s: bytes(int(x,16) for x in s.split(':'))
s=socket.socket(socket.AF_PACKET, socket.SOCK_RAW); s.bind((iface,0))
src=s.getsockname()[4] or mac("02:00:00:00:99:99")
eth=mac(dmac)+src+struct.pack("!H",0x8100)
dot1q=struct.pack("!H",(5<<13)|(vlan&0xfff))+struct.pack("!H",0x0800)
pay=b"DRATSN-RX-WIRE\n"
udp=struct.pack("!HHHH",12345,dport,8+len(pay),0)+pay
# src IP = s-rx talker (part of the rx_flows key, ADR-0027) — else lookup misses.
ip=struct.pack("!BBHHHBBH4s4s",0x45,0,20+len(udp),1,0,64,17,0,
               socket.inet_aton(sip),socket.inet_aton(dip))
# IP 헤더 체크섬 계산 (0으로 두면 rx.c csum_replace가 틀린 base에서 증분 → pod ip_rcv가 drop → UDP 소켓 미수신)
c=sum(struct.unpack("!10H",ip)); c=(c>>16)+(c&0xffff); c+=c>>16; c=(~c)&0xffff
ip=ip[:10]+struct.pack("!H",c)+ip[12:]
frame=eth+dot1q+ip+udp
for _ in range(10): s.send(frame)
print("sent 10x tagged UDP (vlan %d) -> %s:%d"%(vlan,dip,dport))
PY
  pass "inject-rx: sent. Verify in the pod: tcpdump -i eth0 -nn udp (untagged, DNATed)"
}

# capture-tx: capture the vPLC node's TX output arriving on this peer (tagged frame
# from the vPLC node). Run while the vPLC pod is generating its TX flow toward this
# peer (the CNC TX stream's listener must point at this peer's IP/MAC).
capture_tx() {
  hdr "capture-tx — listen on $PEER_IFACE for tagged TX frames (vlan $TX_VLAN)"
  require tcpdump || return 0
  [ -n "$PEER_IFACE" ] || { fail "set PEER_IFACE"; return 0; }
  info "capturing 5 frames (8s) — ensure the vPLC pod is sending TX now..."
  local cap; cap="$(mktemp)"
  timeout 8 tcpdump -i "$PEER_IFACE" -e -nn -c 5 "vlan $TX_VLAN and udp and dst port $TX_DST_PORT" >"$cap" 2>/dev/null
  if grep -q "vlan $TX_VLAN" "$cap"; then
    pass "capture-tx: received tagged TX frame on $PEER_IFACE:"
    grep -m2 "vlan $TX_VLAN" "$cap" | while read -r l; do info "  $l"; done
  else
    fail "capture-tx: no vlan $TX_VLAN UDP on $PEER_IFACE (TX not reaching peer? listener=peer?)"
  fi
  rm -f "$cap"
}

case "${1:-all}" in
  tx) tx_test ;;
  rx) rx_test ;;
  all) tx_test; rx_test ;;
  inject-rx) inject_rx ;;
  capture-tx) capture_tx ;;
  *) echo "usage: $0 [tx|rx|all|inject-rx|capture-tx]"; exit 1 ;;
esac
summary
