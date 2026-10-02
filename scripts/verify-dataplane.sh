#!/usr/bin/env bash
# Scripted RX dataplane verification (docs/e2e_run_book.md §3), driven from the
# control plane (m1). Injects a tagged frame from a peer node (alpha-w2) on the TSN
# segment and confirms it reaches the pod socket -> proves strip + DNAT + redirect +
# correct IP csum. Full received payloads are appended to scripts/results/rx_<ts>.log.
#
# TX is verified MANUALLY on the w1 host (direct `sudo tcpdump` on eno1 / the pod's
# host-veth), not here: m1 can only reach worker NICs via `kubectl debug node`, whose
# tcpdump is unreliable for long captures (see run_book §트러블슈팅). See run_book §1.
#
# Usage: ./verify-dataplane.sh
set -uo pipefail
. "$(dirname "$0")/env.sh"
SD="$(cd "$(dirname "$0")" && pwd)"
RESULTS="$SD/results"; mkdir -p "$RESULTS"
TS="$(date +%Y%m%d-%H%M%S 2>/dev/null || echo run)"

: "${VPLC_POD:=mock-vplc}"
: "${TSN_NODE:=alpha-w1}"
: "${INJECT_NODE:=alpha-w2}"          # peer on the TSN L2 segment for RX injection
: "${RX_ENDPOINT_IP:=192.168.1.1}"; : "${RX_PORT:=4840}"; : "${RX_VLAN:=100}"; : "${RX_TALKER_IP:=192.168.1.11}"  # arm-1 (.11); arm-2=.12

PASS=0; FAIL=0
verdict() { if eval "$2"; then PASS=$((PASS+1)); ok "$1"; else FAIL=$((FAIL+1)); warn "FAIL: $1"; fi; }
gc() { kubectl get pods -A 2>/dev/null | grep node-debugger | awk '{print $1,$2}' \
       | while read -r ns nm; do kubectl delete pod -n "$ns" "$nm" --wait=false >/dev/null 2>&1; done; }
nic_mac() { kubectl get tsnnic "${TSN_NODE}-${TSN_IFACE}" -o jsonpath='{.spec.macAddress}' 2>/dev/null; }
pod_ip()  { kubectl -n "$NS" get pod "$VPLC_POD" -o jsonpath='{.status.podIP}' 2>/dev/null; }

# node_run <node> <short-cmd...> : run a SHORT host command via debug node (long
# debug-node sessions are unreliable, but a quick raw-socket inject is fine).
node_run() {
  local node="$1"; shift
  kubectl debug "node/$node" -i --image=busybox:1.36 --profile=sysadmin -- chroot /host "$@" \
    </dev/null 2>/dev/null | grep -vE 'Creating|Removing'
}

rx_verify() {
  local log="$RESULTS/rx_${TS}.log"; local nicmac pip; nicmac="$(nic_mac)"; pip="$(pod_ip)"
  info "RX: inject vlan $RX_VLAN from $INJECT_NODE -> receive in $VPLC_POD -> $log"
  { echo "# RX verify $TS  inject=$INJECT_NODE w1mac=$nicmac talker=$RX_TALKER_IP endpoint=$RX_ENDPOINT_IP:$RX_PORT vlan=$RX_VLAN pod=$pip"; } >"$log"
  [ -n "$pip" ] || { warn "RX: pod $VPLC_POD has no IP"; FAIL=$((FAIL+1)); return; }

  # pod nc receive (run_book §3 method A) — the reliable proof the frame reaches the
  # pod socket: receiving the payload means strip + DNAT + redirect + correct IP csum
  # all succeeded (RX redirect is invisible to host/eno1 tcpdump — XDP ndo_xdp_xmit
  # bypasses the taps — and an in-pod ephemeral tcpdump leaves a debug container on
  # the pod every run, so nc via `kubectl exec` is the clean, reliable choice).
  echo "=== pod $VPLC_POD nc -u -l :$RX_PORT (received payloads = reaches pod) ===" >>"$log"
  kubectl -n "$NS" exec "$VPLC_POD" -- timeout 20 nc -u -l -p "$RX_PORT" >>"$log" 2>&1 &
  local ncap=$!
  sleep 3   # nc comes up instantly (busybox, no image pull)

  info "RX: injecting from $INJECT_NODE (raw vlan $RX_VLAN, src=$RX_TALKER_IP, dst_mac=$nicmac)"
  local i; for i in 1 2 3; do  # repeat bursts to cover the receive window
    node_run "$INJECT_NODE" python3 -c "
import socket,struct
s=socket.socket(socket.AF_PACKET,socket.SOCK_RAW); s.bind(('$TSN_IFACE',0))
mac=lambda x: bytes(int(b,16) for b in x.split(':'))
src=s.getsockname()[4] or mac('02:00:00:00:99:99')
eth=mac('$nicmac')+src+struct.pack('!H',0x8100)
d1q=struct.pack('!H',(5<<13)|($RX_VLAN&0xfff))+struct.pack('!H',0x0800)
pay=b'sensor data!\n'; udp=struct.pack('!HHHH',12345,$RX_PORT,8+len(pay),0)+pay
ip=struct.pack('!BBHHHBBH4s4s',0x45,0,20+len(udp),1,0,64,17,0,socket.inet_aton('$RX_TALKER_IP'),socket.inet_aton('$RX_ENDPOINT_IP'))
c=sum(struct.unpack('!10H',ip));c=(c>>16)+(c&0xffff);c+=c>>16;c=(~c)&0xffff
ip=ip[:10]+struct.pack('!H',c)+ip[12:]
f=eth+d1q+ip+udp
for _ in range(10): s.send(f)
print('burst: injected 10x vlan%d %s->%s:%d'%($RX_VLAN,'$RX_TALKER_IP','$RX_ENDPOINT_IP',$RX_PORT))" | tee -a "$log"
    sleep 4
  done
  wait "$ncap" 2>/dev/null; gc

  echo "--- check ---" >>"$log"
  local n; n="$(grep -c 'sensor data!' "$log")"; info "RX: pod received $n payloads"
  verdict "RX reaches pod (nc got sensor data!)"      "grep -q 'sensor data!' '$log'"
  verdict "RX strip+DNAT+csum OK (pod socket got it)" "[ '$n' -gt 0 ]"
  { echo "# verdict PASS=$PASS FAIL=$FAIL"; } >>"$log"
}

rx_verify
echo; ok "results in $RESULTS/  (PASS=$PASS FAIL=$FAIL)"
[ "$FAIL" -eq 0 ]
