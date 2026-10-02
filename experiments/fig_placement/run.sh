#!/usr/bin/env bash
# fig_placement: placement correctness + scheduling latency, baseline vs DRATSN.
#
# baseline arm: identical pod WITHOUT a ResourceClaim -> the scheduler is blind
#               to the TSN path and places by compute resources only.
# dratsn arm:   pod + ResourceClaimTemplate (CEL: domainId=='cell-A' &&
#               direction=='TX') -> must land on the node whose ResourceSlice
#               publishes a matching device (= TSN_NODE).
#
# Output: results/<RUN>/placement.csv  rows: arm,trial,node,sched_ms
#   sched_ms = client-side wall time from create to first non-empty
#   .spec.nodeName (kubectl wait watch). Resolution ~100ms (kubectl startup);
#   identical overhead in both arms, so the comparison stands.
#
# Usage: TRIALS=30 ./run.sh          (RUN=<name> to name the results dir)
set -u
cd "$(dirname "$0")"
. ../lib.sh
PHASE="fig_placement"
: "${RUN:=$(date +%Y%m%d-%H%M%S)}"
OUT="results/$RUN"; mkdir -p "$OUT"
CSV="$OUT/placement.csv"

hdr "fig_placement (TRIALS=$TRIALS -> $OUT)"

# --- preflight ---
require kubectl || { summary; exit 1; }
ensure_ns
cnc_pf_start
ensure_streams
cnc_pf_stop

# All 4 devices must be free: the long-lived mock-vplc fixture holds them.
if resource_exists pod mock-vplc -n "$NS"; then
  info "deleting mock-vplc (holds all 4 devices; redeploy later via tests/loop.sh scenario)"
  k delete pod mock-vplc -n "$NS" --wait=true >/dev/null
fi
wait_for 60 claims_gone || { fail "stale ResourceClaims in $NS"; summary; exit 1; }
n="$(device_count)"
[ "$n" -ge 1 ] || { fail "no devices published on $TSN_NODE"; summary; exit 1; }
info "devices on $TSN_NODE: $n"

# --- pod templates ---
pod_manifest() { # $1=pod name  $2=with_claim(0|1)
  cat <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: $1
  namespace: $NS
  labels: {app: exp-placement}
spec:
  restartPolicy: Never
  terminationGracePeriodSeconds: 0
  containers:
  - name: main
    image: busybox:1.36
    command: ["sh","-c","sleep 3600"]
$([ "$2" = 1 ] && cat <<CLAIM
    resources:
      claims: [{name: tsn}]
  resourceClaims:
  - name: tsn
    resourceClaimTemplateName: exp-tx-claim
CLAIM
)
EOF
}

# claim template: attribute selector (matches either TX stream in cell-A)
k apply -f - >/dev/null <<EOF
apiVersion: resource.k8s.io/v1
kind: ResourceClaimTemplate
metadata:
  name: exp-tx-claim
  namespace: $NS
spec:
  spec:
    devices:
      requests:
      - name: tsn-stream
        exactly:
          deviceClassName: $DRIVER
          selectors:
          - cel:
              expression: >-
                device.attributes['$DRIVER'].domainId == 'cell-A' &&
                device.attributes['$DRIVER'].direction == 'TX'
EOF

echo "arm,trial,node,sched_ms" > "$CSV"

run_trial() { # $1=arm(baseline|dratsn) $2=trial
  local arm="$1" trial="$2" pod="exp-place-$1-$2" with=0
  [ "$arm" = dratsn ] && with=1
  local t0 t1 node
  t0=$(now_ms)
  pod_manifest "$pod" "$with" | k create -f - >/dev/null
  if k wait --for=jsonpath='{.spec.nodeName}' "pod/$pod" -n "$NS" --timeout=60s >/dev/null 2>&1; then
    t1=$(now_ms)
    node="$(jget '{.spec.nodeName}' pod "$pod" -n "$NS")"
  else
    t1=$(now_ms); node="UNSCHEDULED"
  fi
  echo "$arm,$trial,$node,$((t1-t0))" >> "$CSV"
  k delete pod "$pod" -n "$NS" --force --grace-period=0 >/dev/null 2>&1
  # dratsn arm: wait for the generated claim to be GC'd so devices free up
  [ "$with" = 1 ] && wait_for 30 claims_gone
}

for i in $(seq 1 "$TRIALS"); do
  run_trial baseline "$i"
  info "baseline $i/$TRIALS -> $(tail -1 "$CSV" | cut -d, -f3,4)"
done
for i in $(seq 1 "$TRIALS"); do
  run_trial dratsn "$i"
  info "dratsn   $i/$TRIALS -> $(tail -1 "$CSV" | cut -d, -f3,4)"
done

k delete resourceclaimtemplate exp-tx-claim -n "$NS" >/dev/null 2>&1

# --- verdict ---
base_w1=$(awk -F, -v n="$TSN_NODE" '$1=="baseline"&&$3==n{c++}END{print c+0}' "$CSV")
dra_w1=$(awk -F, -v n="$TSN_NODE" '$1=="dratsn"&&$3==n{c++}END{print c+0}' "$CSV")
dra_bad=$(awk -F, -v n="$TSN_NODE" '$1=="dratsn"&&$3!=n{c++}END{print c+0}' "$CSV")
info "baseline on $TSN_NODE: $base_w1/$TRIALS ; dratsn on $TSN_NODE: $dra_w1/$TRIALS"
[ "$dra_bad" -eq 0 ] && pass "dratsn placement correctness 100%" \
                     || fail "dratsn mis-placements: $dra_bad"
summary
