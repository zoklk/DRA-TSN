#!/usr/bin/env bash
# fig_regime: scenario A "failover with no spare" (ADR-0035/0037).
#
# A tight-regime cluster (TSN nodes ~90% requested, the slack occupied by IT/AI
# pods) loses a TSN node. Three strategies contend on the utilisation x
# availability plane:
#
#   spare    claims + tsn-scheduler, one TSN node kept empty as a reserve
#            -> fast recovery without preemption, paid for in utilisation
#   vanilla  no claim, default scheduler, PriorityClass present
#            -> preemption fires, but lands the vPLC blind (general node,
#               historian evicted): priority alone is not enough
#   dratsn   claims + tsn-scheduler + PriorityClass
#            -> DRA Filter narrows preemption to surviving TSN nodes: exactly
#               the lowest-priority AI pods there are evicted, vPLC stays on TSN
#
# The one unverified mechanism this driver exists to exercise (ADR-0035 §결과):
# a claim-holding pod acting as *preemptor* must pass the dynamicresources
# Filter inside the PostFilter preemption simulation.
#
# Fault = docker stop <node> (real node death: detection -> taint eviction ->
# reschedule all measured). Recovery is reported stage-decomposed:
# detect / evict / schedule / ready, ms from the docker stop (ADR-0037).
#
# Staged driver (ADR-0037): each stage is an independent subcommand so the
# figure path and a live demo share one code path.
#   ./run.sh setup                one-time fixtures (PCs, claim template, csv)
#   ./run.sh fill <arm>           build the regime for one arm
#   ./run.sh inject-fault         docker stop the node hosting two vPLCs
#   ./run.sh record <arm> <trial> watch + write csv rows + restore the node
#   ./run.sh clean                delete workloads, restore nodes/taints
#   ./run.sh [all]                setup + (fill/inject/record) x arms x trials
#
# Usage: REGIME_TRIALS=10 RUN=paper ./run.sh
set -u
cd "$(dirname "$0")"
. ../lib.sh
PHASE="fig_regime"

# env.sh has set TSN_IFACE to the real testbed's eno1 by now; kind is eth0
# (same forced override as fig_twin/run_fault.sh)
TSN_IFACE="eth0"; export TSN_IFACE

: "${REGIME_TRIALS:=10}"          # env.sh defaults TRIALS=30; too long here
: "${ARMS:=spare vanilla dratsn}"
: "${RUN:=$(date +%Y%m%d-%H%M%S)}"
: "${UTIL_TSN:=90}"               # target requested% on TSN nodes
: "${UTIL_GEN:=90}"               # target on general nodes, before the absorb reserve
: "${VPLC_MCPU:=2000}"            # Guaranteed vPLC request (mock-vplc.yaml shape)
: "${AI_MCPU:=2000}"              # one AI inference pod
: "${CAM_MCPU:=500}"              # the inspection-camera feed gateway
: "${HIST_MCPU:=1000}"            # one historian pod
: "${VPLC_TOL_S:=2}"              # not-ready/unreachable tolerationSeconds
: "${WATCH_TIMEOUT:=240}"
OUT="results/$RUN"; mkdir -p "$OUT"
STATE="$OUT/state.env"
WF="$EXP_PY watch_fault.py"

hdr "fig_regime (TRIALS=$REGIME_TRIALS arms=[$ARMS] -> $OUT)"

# --- preflight --------------------------------------------------------------
require kubectl || { summary; exit 1; }
require docker  || { summary; exit 1; }

# docker-stops nodes: kind-only, same reasoning as fig_twin
CTX="$(k config current-context 2>/dev/null || true)"
case "$CTX" in
  kind-*) info "context $CTX" ;;
  *) die "refusing to run against '$CTX': this docker-stops nodes, so it is kind-only.
       kubectl config use-context kind-dratsn" ;;
esac

ensure_ns

# --- topology (discovered, not assumed — run_fault.sh convention) -----------
TSN_NODES="$(k get nodes -l tsn.dra.net/capable=true -o jsonpath='{range .items[*]}{.metadata.name} {end}')"
TSN_NODES="$(echo $TSN_NODES)"
WORKERS="$(k get nodes -l '!node-role.kubernetes.io/control-plane' -o jsonpath='{range .items[*]}{.metadata.name} {end}')"
WORKERS="$(echo $WORKERS)"
GEN_NODES=""
for n in $WORKERS; do
  case " $TSN_NODES " in *" $n "*) ;; *) GEN_NODES="$GEN_NODES $n" ;; esac
done
GEN_NODES="$(echo $GEN_NODES)"
n_tsn=$(echo "$TSN_NODES" | wc -w); n_gen=$(echo "$GEN_NODES" | wc -w)

[ "$n_tsn" -ge 3 ] || die "need >= 3 TSN nodes (got $n_tsn): one dies, two survive for preemption"
[ "$n_gen" -ge 3 ] || die "need >= 3 general nodes (got $n_gen) to absorb evicted IT pods.
       Rebuild: ../kind/create-cluster.sh with the 6-worker kind-config (ADR-0037)"
# per-node "4 devices (2 cell-A TX)" is asserted in setup(), which can heal a
# node whose streams a previous docker stop broke (see repair_streams)
SPARE_NODE="${TSN_NODES##* }"     # last TSN node doubles as the spare-arm reserve
info "TSN: $TSN_NODES | general: $GEN_NODES | spare-arm reserve: $SPARE_NODE"

is_tsn_node() { case " $TSN_NODES " in *" $1 "*) return 0 ;; *) return 1 ;; esac; }

# kind hardcodes container names (<cluster>-worker, -worker2, ...) while the
# K8s node names carry full numbering via nodeRegistration (worker1..worker6,
# kind/kind-config.yaml) — every docker-facing call maps worker1 back to the
# unnumbered container.
node_container() { case "$1" in *worker1) echo "${1%1}" ;; *) echo "$1" ;; esac; }

# --- DRA prepare health ------------------------------------------------------
# A long-lived kubelet accumulates a wedged DRA state: claim pods stick in
# ContainerCreating with no sandbox request and no error event (observed after
# ~13h and many trials; a kubelet restart heals it). Probe every TSN node
# before a run and self-heal once.
prepare_probe() { # $1=node -> 0 if a claim pod reaches Running quickly
  local n="$1"
  k delete pod -n "$NS" "probe-$n" --force --grace-period=0 >/dev/null 2>&1
  k apply -f - >/dev/null <<PROBEEOF
apiVersion: v1
kind: Pod
metadata: {name: probe-$n, namespace: $NS}
spec:
  nodeSelector: {kubernetes.io/hostname: $n}
  schedulerName: tsn-scheduler
  terminationGracePeriodSeconds: 0
  containers:
  - name: c
    image: busybox:1.36
    command: ["sh","-c","sleep 60"]
    resources: {requests: {cpu: 100m}, claims: [{name: tsn}]}
  resourceClaims: [{name: tsn, resourceClaimTemplateName: regime-tx-claim}]
PROBEEOF
  probe_running() { [ "$(k get pod -n "$NS" "probe-$n" -o jsonpath='{.status.phase}' 2>/dev/null)" = "Running" ]; }
  local rc=0
  wait_for 25 probe_running || rc=1
  k delete pod -n "$NS" "probe-$n" --force --grace-period=0 >/dev/null 2>&1
  return $rc
}

prepare_health() {
  local n
  for n in $TSN_NODES; do
    if prepare_probe "$n"; then pass "prepare probe $n"; continue; fi
    info "claim prepare wedged on $n — restarting kubelet"
    docker exec "$(node_container "$n")" systemctl restart kubelet
    sleep 12
    prepare_probe "$n" || die "claim prepare still wedged on $n after kubelet restart"
    pass "prepare probe $n (after kubelet restart)"
  done
}

# --- stream repair ----------------------------------------------------------
# docker stop/start regenerates the kind node's eth0 MAC, so the CNC streams
# pushed at deploy time stop matching the TSNNic and the controller GCs the
# node's TX TSNStreams ("no longer in CNC/NIC join"). After every node restart
# the CNC copy must be replaced with the fresh MAC. Suffix/acc mirror
# kind/deploy.sh's SFX/ACC arrays (indexed by position in the TSN node list).
SFXS=("" "-b" "-c" "-d" "-e" "-f")
ACCS=(400000 100000 250000 400000 400000 400000)

repair_streams() { # $1=node — delete the node's CNC streams, re-push, wait for 4 devices
  local node="$1" i=0 sfx="" acc=400000 n
  for n in $TSN_NODES; do
    if [ "$n" = "$node" ]; then sfx="${SFXS[$i]}"; acc="${ACCS[$i]}"; fi
    i=$((i+1))
  done
  # the plugin's TSNNic update can lag node Ready; pushing a stale MAC would
  # leave the join broken, so wait until the CR matches the live NIC
  local live_mac; live_mac="$(docker exec "$(node_container "$node")" cat /sys/class/net/eth0/address 2>/dev/null || true)"
  if [ -n "$live_mac" ]; then
    nic_fresh() { [ "$(jget '{.spec.macAddress}' tsnnic "$node-eth0")" = "$live_mac" ]; }
    wait_for 90 nic_fresh || die "TSNNic $node-eth0 MAC not refreshed after restart"
  fi
  local rx_ip; rx_ip="$(jget '{.spec.hostIP}' tsnnic "$node-eth0")"
  cnc_pf_start
  for s in "s-tx$sfx" "s-tx2$sfx" "s-rx$sfx" "s-rx2$sfx"; do
    cnc_curl DELETE "/admin/streams/$s" >/dev/null
  done
  ensure_streams "$node" eth0 "$sfx" "$rx_ip" "$acc"
  cnc_pf_stop
  node_devices_ok() { [ "$(device_count "$node")" -ge 4 ]; }
  wait_for 60 node_devices_ok || die "$node still publishes $(device_count "$node") devices after stream repair"
}

# --- fixtures ---------------------------------------------------------------
setup() {
  # mock-cnc keeps its streams in memory and the controller/scheduler are
  # cluster singletons: any of them dying with the faulted node would break the
  # experiment (an emptied CNC store GCs every TSNStream). Pin them to the
  # control plane, which is never docker-stopped.
  local moved=0 d
  for d in mock-cnc tsn-controller tsn-scheduler; do
    local cur; cur="$(k -n kube-system get deploy "$d" -o jsonpath='{.spec.template.spec.nodeSelector}')"
    case "$cur" in *control-plane*) continue ;; esac
    k -n kube-system patch deployment "$d" --type=strategic -p '{
      "spec": {"template": {"spec": {
        "nodeSelector": {"node-role.kubernetes.io/control-plane": ""},
        "tolerations": [{"key": "node-role.kubernetes.io/control-plane",
                          "operator": "Exists", "effect": "NoSchedule"}]}}}}' >/dev/null
    moved=1
  done
  if [ "$moved" = 1 ]; then
    for d in mock-cnc tsn-controller tsn-scheduler; do
      k -n kube-system rollout status deploy "$d" --timeout=120s >/dev/null
    done
    # moving mock-cnc emptied its in-memory store; the controller GCs the
    # TSNStreams lazily, so a device_count check right now can still see the
    # doomed devices — repush every node instead of trusting the snapshot
    for n in $TSN_NODES; do repair_streams "$n"; done
  else
    # heal any node whose streams a previous aborted fault left broken
    for n in $TSN_NODES; do
      [ "$(device_count "$n")" -ge 4 ] || repair_streams "$n"
    done
  fi
  cat > "$OUT/topology.env" <<EOF
EXP_WORKERS=$((n_tsn + n_gen))
EXP_TSN_COUNT=$n_tsn
EXP_TSN_NODES="$TSN_NODES"
EXP_GEN_NODES="$GEN_NODES"
EXP_UTIL_TSN=$UTIL_TSN
EXP_UTIL_GEN=$UTIL_GEN
EOF

  if resource_exists pod mock-vplc -n "$NS"; then
    info "deleting mock-vplc (holds devices)"
    k delete pod mock-vplc -n "$NS" --wait=true >/dev/null
  fi

  k apply -f manifests/priorityclasses.yaml >/dev/null
  k apply -f - >/dev/null <<EOF
apiVersion: resource.k8s.io/v1
kind: ResourceClaimTemplate
metadata:
  name: regime-tx-claim
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

  [ -f "$OUT/regime.csv" ] || echo "arm,trial,vplc,node0,on_tsn0,node1,on_tsn1,detect_ms,evict_ms,sched_ms,ready_ms,resched_ms,pending" > "$OUT/regime.csv"
  [ -f "$OUT/evictions.csv" ] || echo "arm,trial,pod,priority_class,node,correct,ai_resched_ms" > "$OUT/evictions.csv"
  [ -f "$OUT/util.csv" ] || echo "arm,trial,phase,pool,requests_mcpu,allocatable_mcpu,pct" > "$OUT/util.csv"
  prepare_health
  pass "setup done"
}

# --- manifests --------------------------------------------------------------
# Declared per-tier egress bandwidth. Display/ledger only: the scheduler never
# reads the annotation and kind's CNI does not shape on it — it is the network
# half of the request ledger (build_feed BW_MBPS mirrors these numbers for the
# runs collected before the annotation existed).
bw_of() { # $1=priorityclass/tier -> kubernetes.io/egress-bandwidth value
  case "$1" in
    vplc-*) echo 5M ;; ot-monitoring) echo 150M ;;
    it-ai) echo 150M ;; it-historian) echo 60M ;; *) echo 1M ;;
  esac
}
# vPLC: Guaranteed (requests==limits, integer-ish cpu) like examples/mock-vplc.yaml,
# short not-ready/unreachable tolerations so taint eviction, not the 300 s
# default, bounds the evict stage. Spread over hostnames (2+1+1 across 3 TSN
# nodes) picks the fault target for us — no pinning.
vplc_manifest() { # $1=name $2=priorityclass $3=with_claim(0|1) $4=scheduler("" = default)
  cat <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: $1
  namespace: $NS
  labels: {exp: fig-regime}
spec:
  replicas: 1
  selector:
    matchLabels: {app: $1}
  template:
    metadata:
      labels: {app: $1, exp: fig-regime, tier: $2, vplc-group: regime}
      annotations: {kubernetes.io/egress-bandwidth: "$(bw_of "$2")"}
    spec:
      $([ -n "$4" ] && echo "schedulerName: $4")
      priorityClassName: $2
      terminationGracePeriodSeconds: 0
      tolerations:
      - key: node.kubernetes.io/not-ready
        operator: Exists
        effect: NoExecute
        tolerationSeconds: $VPLC_TOL_S
      - key: node.kubernetes.io/unreachable
        operator: Exists
        effect: NoExecute
        tolerationSeconds: $VPLC_TOL_S
      topologySpreadConstraints:
      - maxSkew: 1
        topologyKey: kubernetes.io/hostname
        whenUnsatisfiable: ScheduleAnyway
        labelSelector:
          matchLabels: {vplc-group: regime}
      # identical in every arm: models the operator's intent "vPLCs belong on
      # TSN nodes". For claim-holding arms the DRA Filter enforces it anyway;
      # for vanilla it wins the initial placement over the soft spread's pull
      # toward empty general nodes (all arms share one pre-fault start), but a
      # preference cannot preempt — at failover the full TSN nodes are simply
      # infeasible and the blind scheduler lands on a general node.
      affinity:
        nodeAffinity:
          preferredDuringSchedulingIgnoredDuringExecution:
          - weight: 100
            preference:
              matchExpressions:
              - key: tsn.dra.net/capable
                operator: In
                values: ["true"]
      containers:
      - name: vplc
        image: busybox:1.36
        command: ["sh","-c","sleep 3600"]
        resources:
          requests: {cpu: "${VPLC_MCPU}m", memory: 512Mi}
          limits:   {cpu: "${VPLC_MCPU}m", memory: 512Mi}
$([ "$3" = 1 ] && cat <<CLAIM
          claims: [{name: tsn}]
      resourceClaims:
      - name: tsn
        resourceClaimTemplateName: regime-tx-claim
CLAIM
)
EOF
}

# The camera-feed gateway: the inspection camera's stream enters the cluster
# through this pod (the AI fill consumes it). It co-locates with the vPLC via
# preferred podAffinity, carries the vplc-group label so the watch records its
# reschedule with the same staged pipeline, and holds no TSN claim — camera
# traffic is best-effort by design (GigE Vision on plain Ethernet).
camfeed_manifest() {
  cat <<CAMEOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: camfeed
  namespace: $NS
  labels: {exp: fig-regime}
spec:
  replicas: 1
  selector:
    matchLabels: {app: camfeed}
  template:
    metadata:
      labels: {app: camfeed, exp: fig-regime, tier: ot-monitoring, vplc-group: regime}
      annotations: {kubernetes.io/egress-bandwidth: "$(bw_of ot-monitoring)"}
    spec:
      priorityClassName: ot-monitoring
      terminationGracePeriodSeconds: 0
      tolerations:
      - key: node.kubernetes.io/not-ready
        operator: Exists
        effect: NoExecute
        tolerationSeconds: $VPLC_TOL_S
      - key: node.kubernetes.io/unreachable
        operator: Exists
        effect: NoExecute
        tolerationSeconds: $VPLC_TOL_S
      affinity:
        podAffinity:
          preferredDuringSchedulingIgnoredDuringExecution:
          - weight: 100
            podAffinityTerm:
              topologyKey: kubernetes.io/hostname
              labelSelector:
                matchLabels: {app: vplc-line}
      containers:
      - name: camfeed
        image: busybox:1.36
        command: ["sh","-c","sleep 3600"]
        resources:
          requests: {cpu: "${CAM_MCPU}m", memory: 128Mi}
CAMEOF
}

# Fillers: one Deployment per node with *preferred* affinity to it. Preferred,
# not required: the initial fill lands where the plan says (the target node is
# the only one with room at its own weight), while a replacement created after
# preemption is free to move — evicted AI must be able to migrate to a general
# node, which is the whole story. cpu-only requests: the puzzle is N x C,
# memory is excluded by assumption (ADR-0035).
filler_manifest() { # $1=name $2=priorityclass $3=node $4=replicas $5=mcpu
  cat <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: $1
  namespace: $NS
  labels: {exp: fig-regime}
spec:
  replicas: $4
  selector:
    matchLabels: {app: $1}
  template:
    metadata:
      labels: {app: $1, exp: fig-regime, tier: $2}
      annotations: {kubernetes.io/egress-bandwidth: "$(bw_of "$2")"}
    spec:
      priorityClassName: $2
      terminationGracePeriodSeconds: 0
      # a pod that declares its own spread constraints opts out of the cluster
      # defaults; without this the default hostname/zone spread (weight 2)
      # overrules the node preference below and scatters the replicas — the
      # per-node fill arithmetic then lands on the wrong nodes entirely
      topologySpreadConstraints:
      - maxSkew: 100
        topologyKey: kubernetes.io/hostname
        whenUnsatisfiable: ScheduleAnyway
        labelSelector:
          matchLabels: {app: $1}
      affinity:
        nodeAffinity:
          preferredDuringSchedulingIgnoredDuringExecution:
          - weight: 100
            preference:
              matchExpressions:
              - key: kubernetes.io/hostname
                operator: In
                values: ["$3"]
      containers:
      - name: filler
        image: busybox:1.36
        command: ["sh","-c","sleep 3600"]
        resources:
          requests: {cpu: "${5}m", memory: 128Mi}
EOF
}

exp_pods_json() { k get pods -n "$NS" -l exp=fig-regime -o json 2>/dev/null; }
# settled = every experiment Deployment reports readyReplicas == replicas.
# Checking pod phases instead races the ReplicaSets: the check can pass while
# half the replicas are not created yet, which let the AI plan run before the
# vPLCs were even bound (observed in the gate run).
all_running() {
  k get deploy -n "$NS" -l exp=fig-regime -o json 2>/dev/null | "$EXP_PY" -c '
import json,sys
items = json.load(sys.stdin).get("items", [])
ok = bool(items) and all(
    d["status"].get("readyReplicas", 0) == d["spec"].get("replicas", 1)
    for d in items)
sys.exit(0 if ok else 1)'
}
count_running() {
  exp_pods_json | "$EXP_PY" -c '
import json,sys
print(sum(1 for p in json.load(sys.stdin).get("items",[]) if p["status"].get("phase")=="Running"))'
}

# fill <arm>: historian (general) -> vPLC -> AI (TSN), in that order. With the
# general nodes already full, even a blind LeastAllocated scheduler starts the
# vPLCs on the then-empty TSN nodes: all arms share the same pre-fault start,
# the difference is confined to the failover (ADR-0037 §맥락).
fill() {
  local arm="$1" with=1 sched="tsn-scheduler" fill_tsn="$TSN_NODES"
  case "$arm" in
    vanilla) with=0; sched="" ;;
    spare)   fill_tsn="${TSN_NODES% $SPARE_NODE}"
             k taint node "$SPARE_NODE" tsn.dra.net/reserved=true:NoSchedule --overwrite >/dev/null ;;
  esac

  # historian fill: hold back room for the evicted AI pod and the camfeed
  local absorb=$(( (AI_MCPU + CAM_MCPU + n_gen - 1) / n_gen ))
  $WF plan --nodes "$GEN_NODES" --util "$UTIL_GEN" --unit-mcpu "$HIST_MCPU" \
      --reserve-mcpu "$absorb" | while read -r node count trim; do
    [ "$count" -gt 0 ] && filler_manifest "hist-$node" it-historian "$node" "$count" "$HIST_MCPU" | k apply -f - >/dev/null
    [ "$trim" -gt 0 ] && filler_manifest "hist-$node-trim" it-historian "$node" 1 "$trim" | k apply -f - >/dev/null
  done
  wait_for 120 all_running || die "historian fill did not settle ($(count_running) running)"

  # fixture v2: one line, one vPLC — plus its camera-feed gateway riding along
  vplc_manifest vplc-line vplc-line "$with" "$sched" | k apply -f - >/dev/null
  wait_for 120 all_running || die "vPLC fill did not settle"
  camfeed_manifest | k apply -f - >/dev/null
  wait_for 120 all_running || die "camfeed did not settle"
  local vn cn
  vn="$(k get pods -n "$NS" -l app=vplc-line -o jsonpath='{.items[0].spec.nodeName}')"
  cn="$(k get pods -n "$NS" -l app=camfeed -o jsonpath='{.items[0].spec.nodeName}')"
  [ -n "$vn" ] && [ "$vn" = "$cn" ] || die "camfeed ($cn) not co-located with vplc-line ($vn)"

  # AI fill on TSN nodes, computed against live requests (vPLCs included).
  # The trimmer pins headroom to exactly (100-UTIL_TSN)% < one vPLC request:
  # without it, unit flooring left enough slack to admit a vPLC un-preempted
  # (observed in the gate run) and the whole scenario silently degrades.
  $WF plan --nodes "$fill_tsn" --util "$UTIL_TSN" --unit-mcpu "$AI_MCPU" \
      | while read -r node count trim; do
    [ "$count" -gt 0 ] && filler_manifest "ai-$node" it-ai "$node" "$count" "$AI_MCPU" | k apply -f - >/dev/null
    [ "$trim" -gt 0 ] && filler_manifest "ai-$node-trim" it-ai "$node" 1 "$trim" | k apply -f - >/dev/null
  done
  wait_for 120 all_running || die "AI fill did not settle"

  [ "$arm" = spare ] && k taint node "$SPARE_NODE" tsn.dra.net/reserved- >/dev/null 2>&1
  pass "fill($arm): $(count_running) pods running"
}

# the node hosting THE vPLC is the fault target (fixture v2: one line, one vPLC)
fault_target() {
  exp_pods_json | "$EXP_PY" -c '
import json,sys
for p in json.load(sys.stdin).get("items", []):
    l = p["metadata"].get("labels", {})
    if l.get("tier","").startswith("vplc") and p["spec"].get("nodeName") \
       and not p["metadata"].get("deletionTimestamp"):
        print(p["spec"]["nodeName"]); break
else: print("")'
}

inject_fault() {
  local node0 t0
  node0="$(fault_target)"
  [ -n "$node0" ] || die "no TSN node hosts 2 vPLCs — fill first (spread should give 2+1+1)"
  info "docker stop $node0"
  t0=$(now_ms)
  docker stop "$(node_container "$node0")" >/dev/null
  cat > "$STATE" <<EOF
NODE0=$node0
T0=$t0
EOF
}

node_is_ready() {
  [ "$(k get node "$1" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)" = "True" ]
}

record() { # $1=arm $2=trial
  local arm="$1" trial="$2"
  . "$STATE"
  $WF watch --arm "$arm" --trial "$trial" --node0 "$NODE0" --t0-ms "$T0" \
      --tsn-nodes "$TSN_NODES" --ns "$NS" --out "$OUT" --timeout "$WATCH_TIMEOUT"
  $WF util --arm "$arm" --trial "$trial" --phase post --tsn-nodes "$TSN_NODES" --csv "$OUT/util.csv"

  docker start "$(node_container "$NODE0")" >/dev/null
  wait_for 120 node_is_ready "$NODE0" || die "$NODE0 did not come back Ready"
  # the restart regenerated the node's MAC; put its CNC streams back in join
  repair_streams "$NODE0"
}

clean() {
  k delete deployment -n "$NS" -l exp=fig-regime --wait=true >/dev/null 2>&1
  # pods that died with the node come back Terminating when the kubelet
  # restarts; they hold their ResourceClaims (and so TX devices) until removed,
  # which would starve the next trial's allocation — force them out.
  k delete pods -n "$NS" -l exp=fig-regime --force --grace-period=0 >/dev/null 2>&1
  wait_for 120 claims_gone || die "ResourceClaims still present after 120s — next trial would under-allocate"
  k taint node "$SPARE_NODE" tsn.dra.net/reserved- >/dev/null 2>&1 || true
}

restore_all() {
  for n in $TSN_NODES; do
    c="$(node_container "$n")"
    [ "$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null)" = "true" ] || docker start "$c" >/dev/null 2>&1
  done
  k taint node "$SPARE_NODE" tsn.dra.net/reserved- >/dev/null 2>&1 || true
}
# The safety net belongs to the long-running `all` mode only: as an
# unconditional EXIT trap it fires when the `inject-fault` subcommand exits —
# restarting the node it just stopped and silently unwinding the fault
# (observed: every staged watch saw a healthy node, det=-1, all PENDING).

run_trial() { # $1=arm $2=trial
  fill "$1"
  $WF util --arm "$1" --trial "$2" --phase pre --tsn-nodes "$TSN_NODES" --csv "$OUT/util.csv"
  inject_fault
  record "$1" "$2"
  clean
}

verdict() {
  "$EXP_PY" - "$OUT" <<'PY'
import csv, sys, collections
out = sys.argv[1]
rows = list(csv.DictReader(open(f"{out}/regime.csv")))
ev = list(csv.DictReader(open(f"{out}/evictions.csv")))
util = list(csv.DictReader(open(f"{out}/util.csv")))
def arm_rows(a):
    # camfeed rows share the staged pipeline but are not vPLC recoveries
    return [r for r in rows if r["arm"] == a and r["vplc"].startswith("vplc")]
def cam_rows(a): return [r for r in rows if r["arm"] == a and r["vplc"] == "camfeed"]
def fail(msg): print(f"VERDICT-FAIL {msg}")
def ok(msg): print(f"VERDICT-PASS {msg}")

d = arm_rows("dratsn")
if d:
    bad = [r for r in d if r["on_tsn1"] != "1" or r["pending"] != "0"]
    (ok if not bad else fail)(f"dratsn: {len(d)-len(bad)}/{len(d)} vPLC recoveries on a TSN node")
    de = [e for e in ev if e["arm"] == "dratsn"]
    wrong = [e for e in de if e["correct"] != "1"]
    trials = {r["trial"] for r in d}
    per = collections.Counter(e["trial"] for e in de)
    (ok if de and not wrong else fail)(f"dratsn: evictions all it-ai ({len(de)-len(wrong)}/{len(de)})")
    # the regime is only tight if every recovery had to preempt: zero evictions
    # means the fill arithmetic left a vPLC-sized hole (the gate-run bug)
    tight = all(per.get(t, 0) == 1 for t in trials)
    (ok if tight else fail)("dratsn: exactly 1 eviction per trial (one vPLC preempts once)")
    cams = cam_rows("dratsn")
    (ok if len(cams) == len(trials) and all(r["pending"] == "0" for r in cams)
     else fail)(f"dratsn: camfeed rescheduled in every trial ({len(cams)}/{len(trials)})")
v = arm_rows("vanilla")
if v:
    off = [r for r in v if r["on_tsn1"] == "0" and r["pending"] == "0"]
    (ok if off else fail)(f"vanilla: {len(off)}/{len(v)} recoveries landed OFF TSN (blind placement visible)")
s = arm_rows("spare")
if s:
    se = [e for e in ev if e["arm"] == "spare"]
    bad = [r for r in s if r["on_tsn1"] != "1" or r["pending"] != "0"]
    (ok if not se else fail)(f"spare: {len(se)} evictions (expected 0)")
    (ok if not bad else fail)(f"spare: {len(s)-len(bad)}/{len(s)} recoveries on the reserve TSN node")
    # the trade-off the figure claims: spare pays for HA in TSN-pool utilisation
    def mean_pre(a):
        xs = [float(u["pct"]) for u in util if u["arm"]==a and u["phase"]=="pre" and u["pool"]=="tsn"]
        return sum(xs)/len(xs) if xs else 0.0
    sp, dr = mean_pre("spare"), mean_pre("dratsn")
    if dr: (ok if sp < dr else fail)(f"spare tsn-pool util {sp:.1f}% < dratsn {dr:.1f}%")
print(f"rows: regime={len(rows)} evictions={len(ev)}")
PY
}

# --- dispatch ---------------------------------------------------------------
case "${1:-all}" in
  setup)        setup ;;
  fill)         setup >/dev/null; fill "${2:?fill <arm>}" ;;
  inject-fault) inject_fault ;;
  repair)       shift; for n in "${@:?repair <node>...}"; do repair_streams "$n"; done ;;
  record)       record "${2:?record <arm> <trial>}" "${3:?record <arm> <trial>}" ;;
  clean)        clean ;;
  all)
    trap restore_all EXIT
    setup
    for arm in $ARMS; do
      hdr "arm $arm"
      for i in $(seq 1 "$REGIME_TRIALS"); do
        run_trial "$arm" "$i"
        info "$arm $i/$REGIME_TRIALS -> $(tail -2 "$OUT/regime.csv" | cut -d, -f3,6,8,9,10,11 | tr '\n' ' ')"
      done
    done
    k delete resourceclaimtemplate regime-tx-claim -n "$NS" >/dev/null 2>&1
    k delete -f manifests/priorityclasses.yaml >/dev/null 2>&1
    verdict | while read -r line; do
      case "$line" in
        VERDICT-PASS*) pass "${line#VERDICT-PASS }" ;;
        VERDICT-FAIL*) fail "${line#VERDICT-FAIL }" ;;
        *) info "$line" ;;
      esac
    done
    info "results: $OUT"
    summary
    ;;
  *) die "usage: $0 [setup|fill <arm>|inject-fault|repair <node>...|record <arm> <trial>|clean|all]" ;;
esac
