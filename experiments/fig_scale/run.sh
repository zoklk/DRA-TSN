#!/usr/bin/env bash
# fig_scale: scheduler-side scalability of TSN-aware placement (KWOK).
#
# kwokctl runs a REAL kube-apiserver + kube-scheduler with N fake nodes (no
# kubelet), the standard tool for large-scale scheduling experiments. Our
# DaemonSet cannot run on fake nodes, so ResourceSlices are injected
# synthetically (they are plain API objects; the scheduler's structured-DRA
# allocation needs no driver at scheduling time). Scope: this measures the
# SCHEDULER's DRA correctness + latency at scale — the controller/publisher
# pipeline is covered by the kind path and the real testbed.
#
# Per node count N in NODES_LIST:
#   - N fake nodes; the first TSN_NODES of them each publish DEV_PER_NODE
#     synthetic TX devices (driver tsn.dra.net, domain cell-A)
#   - batch-create CLAIM_PODS pods with a TSN claim -> measure makespan
#     (create -> all scheduled, 100ms poll) + placement correctness
#   - batch-create the same number of claimless pods -> baseline makespan
#   - one extra claim pod beyond capacity -> must stay Pending
#
# Output: results/<RUN>/scale.csv
#   rows: nodes,arm,pods,makespan_ms,on_tsn,mispl,overflow_pending
# Usage: NODES_LIST="10 100 1000" ./run.sh
set -u
cd "$(dirname "$0")"
. ../lib.sh
PHASE="fig_scale"
: "${RUN:=$(date +%Y%m%d-%H%M%S)}"
: "${NODES_LIST:=10 100 1000}"
: "${TSN_NODES:=5}"          # fake nodes that publish TSN devices
: "${DEV_PER_NODE:=2}"       # TX devices per TSN node
: "${CLAIM_PODS:=10}"        # == TSN_NODES * DEV_PER_NODE (full capacity)
: "${KWOK_NAME:=dratsn-scale}"
: "${KUBE_VERSION:=v1.34.3}"
OUT="results/$RUN"; mkdir -p "$OUT"
CSV="$OUT/scale.csv"
KC="kubectl --context kwok-$KWOK_NAME"

hdr "fig_scale (nodes: $NODES_LIST -> $OUT)"
command -v kwokctl >/dev/null || { fail "kwokctl not found"; summary; exit 1; }

if ! kwokctl get clusters 2>/dev/null | grep -qx "$KWOK_NAME"; then
  info "creating kwok cluster ($KUBE_VERSION)"
  KWOK_KUBE_VERSION="$KUBE_VERSION" kwokctl create cluster --name "$KWOK_NAME" --wait 120s >/dev/null 2>&1 \
    || { fail "kwokctl create cluster failed"; summary; exit 1; }
fi

$KC apply -f ../../deployments/deviceclass.yaml >/dev/null

node_manifest() { # $1=name
  cat <<EOF
---
apiVersion: v1
kind: Node
metadata:
  name: $1
  annotations: {kwok.x-k8s.io/node: fake}
  labels: {type: kwok, kubernetes.io/os: linux, kubernetes.io/hostname: $1}
status:
  allocatable: {cpu: "32", memory: 256Gi, pods: "110"}
  capacity: {cpu: "32", memory: 256Gi, pods: "110"}
  nodeInfo: {kubeletVersion: fake}
EOF
}
slice_manifest() { # $1=node
  cat <<EOF
---
apiVersion: resource.k8s.io/v1
kind: ResourceSlice
metadata:
  name: $1-tsn
spec:
  driver: tsn.dra.net
  nodeName: $1
  pool: {name: $1, generation: 1, resourceSliceCount: 1}
  devices:
$(for d in $(seq 1 "$DEV_PER_NODE"); do cat <<DEV
  - name: s-tx$d-$1-eth0-tx
    attributes:
      streamId: {string: s-tx$d}
      ifName: {string: eth0}
      direction: {string: "TX"}
      domainId: {string: cell-A}
DEV
done)
EOF
}
pod_manifest() { # $1=name $2=with_claim
  cat <<EOF
---
apiVersion: v1
kind: Pod
metadata:
  name: $1
  labels: {app: scale-exp}
spec:
  restartPolicy: Never
  containers:
  - name: main
    image: fake
    resources:
      requests: {cpu: 100m, memory: 64Mi}
$([ "$2" = 1 ] && cat <<CLAIM
      claims: [{name: tsn}]
  resourceClaims:
  - name: tsn
    resourceClaimTemplateName: scale-tx-claim
CLAIM
)
EOF
}

$KC apply -f - >/dev/null <<EOF
apiVersion: resource.k8s.io/v1
kind: ResourceClaimTemplate
metadata: {name: scale-tx-claim}
spec:
  spec:
    devices:
      requests:
      - name: tsn-stream
        exactly:
          deviceClassName: tsn.dra.net
          selectors:
          - cel:
              expression: >-
                device.attributes['tsn.dra.net'].domainId == 'cell-A' &&
                device.attributes['tsn.dra.net'].direction == 'TX'
EOF

echo "nodes,arm,pods,makespan_ms,on_tsn,mispl,overflow_pending" > "$CSV"
CUR_NODES=0

run_arm() { # $1=N $2=arm(dratsn|baseline)
  local N="$1" arm="$2" with=0 prefix="sp-$2"
  [ "$arm" = dratsn ] && with=1
  local t0 t1 i
  { for i in $(seq 1 "$CLAIM_PODS"); do pod_manifest "$prefix-$i" "$with"; done; } > /tmp/scale-pods.yaml
  t0=$(now_ms)
  $KC create -f /tmp/scale-pods.yaml >/dev/null
  local all_done=0
  while [ $(( $(now_ms) - t0 )) -lt 120000 ]; do
    local scheduled
    scheduled=$($KC get pods -l app=scale-exp -o json 2>/dev/null \
      | "$EXP_PY" -c 'import json,sys;d=json.load(sys.stdin);print(sum(1 for p in d["items"] if p["spec"].get("nodeName")))')
    [ "$scheduled" = "$CLAIM_PODS" ] && { all_done=1; break; }
    sleep 0.1
  done
  t1=$(now_ms)
  local on_tsn mispl
  on_tsn=$($KC get pods -l app=scale-exp -o json | "$EXP_PY" -c '
import json,sys
d=json.load(sys.stdin); tsn={"kwok-node-%d"%i for i in range('"$TSN_NODES"')}
print(sum(1 for p in d["items"] if p["spec"].get("nodeName") in tsn))')
  mispl=$($KC get pods -l app=scale-exp -o json | "$EXP_PY" -c '
import json,sys
d=json.load(sys.stdin); tsn={"kwok-node-%d"%i for i in range('"$TSN_NODES"')}
print(sum(1 for p in d["items"] if p["spec"].get("nodeName") and p["spec"]["nodeName"] not in tsn))')

  # overflow: one extra claim pod beyond capacity must stay Pending (dratsn arm only)
  local overflow=NA
  if [ "$arm" = dratsn ]; then
    pod_manifest sp-overflow 1 | $KC create -f - >/dev/null
    sleep 5
    local ofn; ofn=$($KC get pod sp-overflow -o jsonpath='{.spec.nodeName}' 2>/dev/null)
    if [ -z "$ofn" ]; then overflow=yes; else overflow=no; fi
    $KC delete pod sp-overflow --wait=false >/dev/null 2>&1
  fi

  [ "$all_done" = 1 ] || info "WARN: not all pods scheduled within 120s ($arm N=$N)"
  echo "$N,$arm,$CLAIM_PODS,$((t1-t0)),$on_tsn,$mispl,$overflow" >> "$CSV"
  info "N=$N $arm: makespan=$((t1-t0))ms on_tsn=$on_tsn/$CLAIM_PODS mispl=$mispl overflow_pending=$overflow"
  $KC delete pods -l app=scale-exp --wait=false >/dev/null 2>&1
  local gone
  while :; do
    gone=$($KC get pods -l app=scale-exp -o name 2>/dev/null | wc -l)
    [ "$gone" -eq 0 ] && break; sleep 0.5
  done
  wait_for 30 sh -c "[ -z \"\$($KC get resourceclaims -o name 2>/dev/null)\" ]" || true
}

for N in $NODES_LIST; do
  hdr "scale to $N nodes"
  if [ "$N" -gt "$CUR_NODES" ]; then
    { for i in $(seq "$CUR_NODES" $((N-1))); do node_manifest "kwok-node-$i"; done; } > /tmp/scale-nodes.yaml
    $KC create -f /tmp/scale-nodes.yaml >/dev/null
    CUR_NODES="$N"
  fi
  ready() { [ "$($KC get nodes --no-headers 2>/dev/null | awk '$2=="Ready"' | wc -l)" -ge "$N" ]; }
  wait_for 120 ready || { fail "nodes not Ready at N=$N"; break; }
  # (re)apply slices for the TSN nodes — idempotent
  { for i in $(seq 0 $((TSN_NODES-1))); do slice_manifest "kwok-node-$i"; done; } | $KC apply -f - >/dev/null
  run_arm "$N" baseline
  run_arm "$N" dratsn
done

if [ "${KEEP_CLUSTER:-0}" != 1 ]; then
  info "deleting kwok cluster (KEEP_CLUSTER=1 to keep)"
  kwokctl delete cluster --name "$KWOK_NAME" >/dev/null 2>&1
fi

column -t -s, "$CSV"
bad=$(awk -F, 'NR>1 && $2=="dratsn" && ($6!="0" || $7=="no"){c++} END{print c+0}' "$CSV")
[ "$bad" -eq 0 ] && pass "dratsn: 0 misplacements, overflow contained at every scale" \
                 || fail "$bad scale points with misplacement/overflow leak"
summary
