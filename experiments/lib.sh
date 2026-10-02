#!/usr/bin/env bash
# Common helpers for experiments/. Reuses tests/lib.sh (pass/fail/skip/summary,
# k(), wait_for, jget ...) and adds ssh + timing + mock-cnc helpers.

_EXP_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$_EXP_LIB_DIR/env.sh"
. "$_EXP_LIB_DIR/../tests/lib.sh"

# --- ssh to worker nodes (password via SSH_PASS, else key auth) ---
# node_ssh <ip> <cmd...>
node_ssh() {
  local ip="$1"; shift
  if [ -n "$SSH_PASS" ]; then
    SSHPASS="$SSH_PASS" sshpass -e ssh -o StrictHostKeyChecking=accept-new "$SSH_USER@$ip" "$@"
  else
    ssh -o StrictHostKeyChecking=accept-new "$SSH_USER@$ip" "$@"
  fi
}
# node_sudo <ip> <cmd string> : run as root on node (sudo password = SSH_PASS,
# fed via stdin so it never appears on the remote command line)
node_sudo() {
  local ip="$1"; shift
  printf '%s\n' "$SSH_PASS" | node_ssh "$ip" "sudo -S -p '' bash -c $(printf '%q' "$*")"
}
# node_scp <ip>:<remote> <local>  /  <local> <ip>:<remote>
node_scp() {
  if [ -n "$SSH_PASS" ]; then
    SSHPASS="$SSH_PASS" sshpass -e scp -o StrictHostKeyChecking=accept-new "$@"
  else
    scp -o StrictHostKeyChecking=accept-new "$@"
  fi
}

# --- timing ---
now_ms() { date +%s%3N; }

# --- mock-cnc admin API via port-forward ---
# cnc_pf_start / cnc_pf_stop / cnc_curl <method> <path> [json]
CNC_PF_PID=""; CNC_PORT="${CNC_PORT:-18080}"
cnc_pf_start() {
  # refuse a stale port-forward from an earlier run: it may point at another cluster
  if curl -s -o /dev/null --max-time 1 "http://localhost:$CNC_PORT/healthz" 2>/dev/null; then
    die "port $CNC_PORT already serving — kill the stale 'kubectl port-forward' first (ss -tlnp | grep $CNC_PORT)"
  fi
  # exec so \$! is the kubectl pid itself (not a wrapper subshell) and stop kills it
  ( exec "$KUBECTL" -n "$MOCK_CNC_NS" port-forward "svc/$MOCK_CNC_NAME" "$CNC_PORT:8080" >/dev/null 2>&1 ) &
  CNC_PF_PID=$!
  wait_for 10 curl -s -o /dev/null "http://localhost:$CNC_PORT/healthz" || die "mock-cnc port-forward failed"
}
cnc_pf_stop() { [ -n "$CNC_PF_PID" ] && kill "$CNC_PF_PID" 2>/dev/null; CNC_PF_PID=""; }
cnc_curl() {
  local m="$1" p="$2" body="${3:-}"
  if [ -n "$body" ]; then
    curl -s -X "$m" "http://localhost:$CNC_PORT$p" -H 'Content-Type: application/json' -d "$body"
  else
    curl -s -X "$m" "http://localhost:$CNC_PORT$p"
  fi
}
# --- cluster state helpers ---
# device_count [node] : devices currently published for a node (default TSN_NODE)
device_count() {
  local node="${1:-$TSN_NODE}"
  k get resourceslices -o json 2>/dev/null \
    | "$EXP_PY" -c 'import json,sys;d=json.load(sys.stdin);print(sum(len(s["spec"].get("devices",[]) or []) for s in d["items"] if s["spec"].get("pool",{}).get("name")=="'"$node"'"))'
}
# device_names [node] : list device names in a node's pool (default TSN_NODE)
device_names() {
  local node="${1:-$TSN_NODE}"
  k get resourceslices -o json 2>/dev/null \
    | "$EXP_PY" -c 'import json,sys;d=json.load(sys.stdin);[print(x["name"]) for s in d["items"] if s["spec"].get("pool",{}).get("name")=="'"$node"'" for x in s["spec"].get("devices",[]) or []]'
}
# claims_gone : no ResourceClaims left in NS
claims_gone() { [ -z "$(k get resourceclaims -n "$NS" -o name 2>/dev/null)" ]; }

# ensure_streams [node] [iface] [suffix] [rx_endpoint_ip] [accumulated_latency_ns]
#   verify the 4 canonical cell-A streams exist in mock-cnc, restore missing.
#   Defaults reproduce the original single-node behaviour. A second TSN node gets
#   the same cell-A domain under distinct stream ids (suffix), so one CEL selector
#   on domainId matches devices on both nodes (fig_twin needs a reschedule target).
#   The 5th arg declares the CNC path's accumulated-latency for this node's
#   streams; slack = MAX_LATENCY_NS - acc feeds the tsn-scheduler score only.
ensure_streams() {
  local node="${1:-$TSN_NODE}" iface="${2:-$TSN_IFACE}" sfx="${3:-}"
  local rx_ip="${4:-$RX_ENDPOINT_IP}" acc="${5:-0}"
  local nicmac; nicmac="$(jget '{.spec.macAddress}' "tsnnic" "$node-$iface")"
  [ -n "$nicmac" ] || die "TSNNic $node-$iface not found"
  local have; have="$(cnc_curl GET /restconf/data/tsn-streams)"
  # field layout mirrors tests/loop.sh scenario exactly:
  # TX: talker={mac:nicmac}, listener={ip:arm,mac:armmac}, port 4840, vlan 200/pcp 6
  # RX: talker={ip:arm}, listener={ip:endpoint}, port 4840(arm1)/4841(arm2), vlan 100/pcp 5
  for s in s-tx:tx:4840:1 s-tx2:tx:4840:2 s-rx:rx:4840:1 s-rx2:rx:4841:2; do
    local id dir port arm; IFS=: read -r id dir port arm <<<"$s"
    id="$id$sfx"
    echo "$have" | grep -q "\"$id\"" && continue
    local aip amac
    if [ "$arm" = 1 ]; then aip="$ARM1_IP"; amac="$ARM1_MAC"; else aip="$ARM2_IP"; amac="$ARM2_MAC"; fi
    info "restoring stream $id in mock-cnc (node $node)"
    if [ "$dir" = tx ]; then
      cnc_curl POST /admin/streams "{\"streamId\":\"$id\",\"talker\":{\"mac\":\"$nicmac\"},\"listener\":{\"ip\":\"$aip\",\"mac\":\"$amac\"},\"port\":$port,\"vlanId\":$TX_VLAN,\"pcp\":$TX_PCP,\"domainId\":\"cell-A\",\"maxLatency\":$MAX_LATENCY_NS,\"accumulatedLatency\":$acc}" >/dev/null
    else
      cnc_curl POST /admin/streams "{\"streamId\":\"$id\",\"talker\":{\"ip\":\"$aip\"},\"listener\":{\"ip\":\"$rx_ip\"},\"port\":$port,\"vlanId\":$RX_VLAN,\"pcp\":5,\"domainId\":\"cell-A\",\"maxLatency\":$MAX_LATENCY_NS,\"accumulatedLatency\":$acc}" >/dev/null
    fi
  done
}
