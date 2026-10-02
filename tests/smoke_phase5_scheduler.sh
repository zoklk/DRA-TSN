#!/usr/bin/env bash
# Phase 5 smoke: tsn-scheduler (TSNSlackScore + soft topology spread).
# Verifies, against live TSNStream CRs (no hardcoded winner):
#   1) a slack-scheduled pod lands on the max-slack TSN node
#   2) a second pod lands on a DIFFERENT node — PodTopologySpread(weight 100)
#      dominates slack(weight 1), proving the lexicographic weight separation
#   3) the default scheduler is unaffected
# Needs >= 2 TSN nodes with TX streams carrying maxLatencyNs (kind deploy.sh
# pushes per-node accumulated latencies). SKIP when prerequisites are absent.
set -u
cd "$(dirname "$0")"; . ./lib.sh
PHASE="phase5-scheduler"
: "${SCHED_NAME:=tsn-scheduler}"
: "${SCHED_NS:=kube-system}"

hdr "Phase 5 — tsn-scheduler (slack score + spread)"
require kubectl || { summary; exit 0; }

# 0) prerequisites
if ! resource_exists deploy "$SCHED_NAME" -n "$SCHED_NS"; then
  skip "$SCHED_NAME Deployment absent"; summary; exit 0
fi
if wait_for 30 deploy_available "$SCHED_NAME" "$SCHED_NS"; then
  pass "$SCHED_NAME Deployment available"
else fail "$SCHED_NAME present but not available"; summary; exit 1; fi

tsn_nodes="$(k get nodes -l tsn.dra.net/capable=true -o jsonpath='{.items[*].metadata.name}' 2>/dev/null)"
n_tsn=$(echo "$tsn_nodes" | wc -w)
if [ "$n_tsn" -lt 2 ]; then
  skip "need >= 2 TSN nodes for placement assertions (have $n_tsn)"; summary; exit 0
fi

# expected winner(s): per-node max slack over TX streams with a latency budget
slack_table="$(k get "tsnstreams.${APIGROUP}" -o jsonpath='{range .items[*]}{.spec.direction}{" "}{.spec.nodeName}{" "}{.spec.maxLatencyNs}{" "}{.spec.accumulatedLatencyNs}{"\n"}{end}' 2>/dev/null |
  awk '$1=="TX" && $3+0>0 { s=$3-$4; if (s<0) s=0; if (s>best[$2]) best[$2]=s }
       END { for (n in best) print n, best[n] }')"
if [ -z "$slack_table" ]; then
  skip "no TX TSNStream with maxLatencyNs — latency data not pushed"; summary; exit 0
fi
winners="$(echo "$slack_table" | sort -k2 -rn | awk 'NR==1{m=$2} $2==m{print $1}')"
info "per-node slack:"; echo "$slack_table" | sed 's/^/  /'
info "expected winner(s): $winners"

ensure_ns

sched_pod() { # name [schedulerName-line]
  local name="$1" sched="${2:-}"
  k apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: $name
  namespace: $NS
  labels: { app: vplc-p5 }
spec:
  $sched
  restartPolicy: Never
  topologySpreadConstraints:
    - maxSkew: 1
      topologyKey: kubernetes.io/hostname
      whenUnsatisfiable: ScheduleAnyway
      labelSelector:
        matchLabels: { app: vplc-p5 }
  containers:
    - name: vplc
      image: busybox:1.36
      command: ["sh", "-c", "sleep 3600"]
      resources:
        claims: [{ name: tsn }]
  resourceClaims:
    - name: tsn
      resourceClaimTemplateName: tsn-tx-cell-a-p5
EOF
}

cleanup() {
  k -n "$NS" delete pod vplc-p5-a vplc-p5-b vplc-p5-default --ignore-not-found --force --grace-period=0 >/dev/null 2>&1
  k -n "$NS" delete resourceclaimtemplate tsn-tx-cell-a-p5 --ignore-not-found >/dev/null 2>&1
}
cleanup

k apply -f - >/dev/null <<EOF
apiVersion: resource.k8s.io/v1
kind: ResourceClaimTemplate
metadata:
  name: tsn-tx-cell-a-p5
  namespace: $NS
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

pod_node() { jget "{.spec.nodeName}" pod "$1" -n "$NS" 2>/dev/null; }
pod_scheduled() { [ -n "$(pod_node "$1")" ]; }

# 1) slack: lone pod -> max-slack node
sched_pod vplc-p5-a "schedulerName: $SCHED_NAME"
if wait_for 90 pod_scheduled vplc-p5-a; then
  node_a="$(pod_node vplc-p5-a)"
  if echo "$winners" | grep -qx "$node_a"; then
    pass "slack: vplc-p5-a landed on max-slack node $node_a"
  else fail "slack: vplc-p5-a on $node_a, expected one of: $(echo $winners)"; fi
else fail "vplc-p5-a not scheduled by $SCHED_NAME in 90s"; node_a=""; fi

# 2) spread dominance: second pod must avoid pod-a's node even though that
#    node still has max slack (weight 100 spread > weight 1 slack)
sched_pod vplc-p5-b "schedulerName: $SCHED_NAME"
if wait_for 90 pod_scheduled vplc-p5-b; then
  node_b="$(pod_node vplc-p5-b)"
  if [ -n "$node_a" ] && [ "$node_b" != "$node_a" ]; then
    pass "spread dominance: vplc-p5-b landed on $node_b (!= $node_a)"
  elif [ -z "$node_a" ]; then skip "spread check skipped (pod-a unscheduled)"
  else fail "spread dominance: vplc-p5-b co-located on $node_b despite spread weight 100"; fi
else fail "vplc-p5-b not scheduled by $SCHED_NAME in 90s"; fi

# 3) default scheduler unaffected
sched_pod vplc-p5-default ""
if wait_for 90 pod_scheduled vplc-p5-default; then
  pass "default scheduler still places claim pods (on $(pod_node vplc-p5-default))"
else fail "default-scheduled claim pod not placed — default path broken?"; fi

cleanup
summary
