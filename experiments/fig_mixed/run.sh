#!/usr/bin/env bash
# fig_mixed: heterogeneous-resource placement under path loss (ADR-0039).
#
# Supersedes fig_regime, which is preserved as a record. The event is narrowed
# from "node death -> eviction cascade" to "path loss -> replacement accuracy",
# and the whole experiment answers one question:
#
#   Path redundancy can be bought with reservations, but WHICH reservation is
#   still alive cannot be known without a resource model.
#
# Both arms run a properly HA'd cluster: each TSN workload's stream is
# pre-provisioned on k of the M TSN nodes. One of those k nodes dies.
#
#   vanilla  node-label required nodeAffinity + hardcoded stream env; GPU via DRA
#            -> the surviving TSN nodes are indistinguishable by label, so the
#               scheduler picks on CPU score and is right (k-1)/(M-1) of the time
#   dratsn   DRA claim for the stream + tsn-scheduler; GPU via DRA
#            -> only the surviving device is published, so the Filter decides
#
# The asymmetry is the point (ADR-0039 §3): in the SAME cluster and the same Pod,
# GPU — which is modelled as a resource in both arms — is placed correctly by
# both, and only TSN — modelled as a label in one arm — goes wrong. The variable
# is isolated to "was it resourced?".
#
# Fault = docker stop <node> (real death: detection -> taint eviction ->
# reschedule). Detection is an arm-invariant kube constant and is NEVER compared
# between arms (ADR-0039 §10); the kind cluster tunes it down to ~8 s only so the
# stopped interval of the production waveform is a realistic length.
#
# LIVE-FIRST. The scene shows the cluster as it is; this driver is not a
# collector that gets replayed. The reproducible artifact is the ACTION
# SEQUENCE, not the outcome — vanilla lands differently from cycle to cycle and
# that variance is exactly what the demo shows.
#
#   ./run.sh setup                one-time fixtures (PCs, claim templates, csv)
#   ./run.sh streams              provision the HA stream layout in mock-cnc
#   ./run.sh fill <arm>           build the regime, pinning the initial placement
#   ./run.sh inject-fault         docker stop the node hosting vplc-line
#   ./run.sh record <arm> <cycle> watch + write csv rows + restore the node
#   ./run.sh clean                delete workloads, restore nodes/taints
#   ./run.sh [all]                setup + (fill/inject/record) x arms x cycles
#
# Usage: MIXED_CYCLES=10 RUN=paper ./run.sh
set -u
cd "$(dirname "$0")"
. ../lib.sh
PHASE="fig_mixed"

# env.sh has set TSN_IFACE to the real testbed's eno1 by now; kind is eth0
# (same forced override as fig_twin/run_fault.sh)
TSN_IFACE="eth0"; export TSN_IFACE

: "${MIXED_CYCLES:=10}"           # env.sh defaults TRIALS=30; too long here
: "${ARMS:=vanilla dratsn}"       # ADR-0039 §3: spare is retired, physical PLC is declared
: "${RUN:=$(date +%Y%m%d-%H%M%S)}"
: "${UTIL_TSN:=90}"               # target requested% on TSN nodes
: "${UTIL_GEN:=90}"               # target on general nodes, before the absorb reserve

# The four workloads (ADR-0039 §1). Two axes: required resource x preemptibility.
# The (TSN, elastic) cell is empty on purpose — "control cannot be elastic" is a
# claim, not an omission, and the paper states it.
: "${VPLC_MCPU:=2000}"            # vplc-line     TSN        guaranteed
: "${INFER_MCPU:=2000}"           # ai-inference  TSN + GPU  guaranteed  <- lowest freedom
: "${TRAIN_MCPU:=1500}"           # ai-training   GPU        elastic
: "${LOG_MCPU:=1000}"             # logging       none       elastic     (absorber)

: "${VPLC_TOL_S:=2}"              # not-ready/unreachable tolerationSeconds
: "${WATCH_TIMEOUT:=240}"

# --- HA stream layout (ADR-0039 §2) -----------------------------------------
# Each TSN workload's stream is pre-provisioned on k of the M TSN nodes, which is
# what a competent operator running HA would do. One of those k dies.
#
#   domain   workload       provisioned on        k / M
#   cell-A   vplc-line      w1, w2                2 / 3
#   cell-B   ai-inference   w2, w3                2 / 2   (TSN AND GPU)
#
# The dying node is w2, and only vplc-line lives there. ai-inference sits on w3
# from the start and is untouched by the fault — see fill() for why that
# separation is mandatory rather than cosmetic.
#
#   vplc-line:    dratsn sees only the surviving cell-A device (w1) -> correct.
#                 vanilla sees {w1,w3} as label-identical, both ~90% full, both
#                 needing the same preemption -> (k-1)/(M-1) = 1/2.
#   ai-inference: admitted only to the TSN+GPU pair, in BOTH arms, because GPU is
#                 a modelled resource either way. That is the control: the same
#                 pod gets one resource right and the other wrong, and the only
#                 difference between them is whether it was resourced.
#
# A separate domainId per workload is what lets one CEL selector mean "a stream
# for THIS workload, on whichever node still has one". The publisher exposes
# domainId as a device attribute, so no driver change is needed.
: "${DOMAIN_VPLC:=cell-A}"
: "${DOMAIN_INFER:=cell-B}"

# The second DRA driver (../kind/deploy-gpu.sh). Its DeviceClass is named after
# the driver, same convention as ours.
: "${GPU_DRIVER:=gpu.example.com}"
# Diagnostic knob only. Set 0 to build the identical fixture with every GPU claim
# removed, which isolates "does a second registered DRA driver change the claim
# prepare path?" from everything else. Node labels are untouched, so the
# topology and the pinning are the same.
: "${WITH_GPU:=1}"

# Domain ids keep their CNC spelling (cell-A) because that is what the CEL
# selector compares against, but a K8s object name must be an RFC 1123 subdomain
# — lowercase only. One helper so the two spellings never drift.
claim_name() { echo "mixed-tx-$1" | tr 'A-Z' 'a-z'; }
OUT="results/$RUN"; mkdir -p "$OUT"
STATE="$OUT/state.env"
WF="$EXP_PY watch_fault.py"

hdr "fig_mixed (CYCLES=$MIXED_CYCLES arms=[$ARMS] -> $OUT)"

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

# GPU is the second DRA driver (gpu.example.com, ../kind/deploy-gpu.sh). The
# label is only how the DaemonSet is placed; the scheduler sees actual devices.
GPU_NODES="$(k get nodes -l gpu.example.com/present=true -o jsonpath='{range .items[*]}{.metadata.name} {end}')"
GPU_NODES="$(echo $GPU_NODES)"
is_tsn_node() { case " $TSN_NODES " in *" $1 "*) return 0 ;; *) return 1 ;; esac; }
is_gpu_node() { case " $GPU_NODES " in *" $1 "*) return 0 ;; *) return 1 ;; esac; }

# The contested pair: nodes that are both TSN and GPU. ai-inference can only ever
# live here, which is what makes it the least-free workload (2 of 6 nodes).
TSN_GPU_NODES=""
for n in $TSN_NODES; do is_gpu_node "$n" && TSN_GPU_NODES="$TSN_GPU_NODES $n"; done
TSN_GPU_NODES="$(echo $TSN_GPU_NODES)"

[ "$n_tsn" -ge 3 ] || die "need >= 3 TSN nodes (got $n_tsn): M=3 is what makes (k-1)/(M-1)=1/2"
[ "$n_gen" -ge 1 ] || die "need >= 1 general node (got $n_gen) to absorb displaced elastic pods"
[ "$(echo "$TSN_GPU_NODES" | wc -w)" -eq 2 ] || die \
  "need exactly 2 TSN+GPU nodes (got '$TSN_GPU_NODES').
       Run ../kind/deploy-gpu.sh — it labels worker2..5 with gpu.example.com/present."

# Named roles, derived not assumed, but fixed for the whole run so every cycle is
# identical (ADR-0039 §개정: the procedure is reproducible, the outcome is not).
NODE_FAULT="${TSN_GPU_NODES%% *}"        # w2 — holds cell-A AND cell-B; this one dies
NODE_SURV_GPU="${TSN_GPU_NODES##* }"     # w3 — the only surviving TSN+GPU node
NODE_TSN_ONLY=""                         # w1 — TSN without GPU; the other cell-A holder
for n in $TSN_NODES; do
  [ "$n" = "$NODE_FAULT" ] && continue
  [ "$n" = "$NODE_SURV_GPU" ] && continue
  NODE_TSN_ONLY="$n"; break
done
[ -n "$NODE_TSN_ONLY" ] || die "no TSN-only node left after picking the TSN+GPU pair"

# The whole experiment in four lines.
info "TSN: $TSN_NODES | GPU: $GPU_NODES | general: $GEN_NODES"
info "  $DOMAIN_VPLC  (vplc-line)    -> $NODE_TSN_ONLY $NODE_FAULT"
info "  $DOMAIN_INFER (ai-inference) -> $NODE_FAULT $NODE_SURV_GPU"
info "  fault -> $NODE_FAULT ; after it dies vplc-line is only correct on $NODE_TSN_ONLY,"
info "           and vanilla cannot tell $NODE_TSN_ONLY from $NODE_SURV_GPU"

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
prepare_probe() { # $1=node $2=claim template -> 0 if a claim pod runs quickly
  local n="$1" tmpl="$2"
  k delete pod -n "$NS" "probe-$n" --force --grace-period=0 >/dev/null 2>&1
  k apply -f - >/dev/null <<PROBEEOF
apiVersion: v1
kind: Pod
metadata: {name: probe-$n, namespace: $NS}
spec:
  nodeSelector: {kubernetes.io/hostname: $n}
  terminationGracePeriodSeconds: 0
  containers:
  - name: c
    image: busybox:1.36
    command: ["sh","-c","sleep 60"]
    resources: {requests: {cpu: 100m}, claims: [{name: c}]}
  resourceClaims: [{name: c, resourceClaimTemplateName: $tmpl}]
PROBEEOF
  probe_running() { [ "$(k get pod -n "$NS" "probe-$n" -o jsonpath='{.status.phase}' 2>/dev/null)" = "Running" ]; }
  local rc=0 phase
  wait_for 25 probe_running || rc=1
  # Distinguish the two ways a claim pod fails to run. Pending means the
  # scheduler found no free device — a capacity fact, and restarting kubelet
  # would be both useless and destructive. The wedge we are hunting is the
  # opposite: bound to a node, stuck in ContainerCreating, no sandbox requested
  # and no event. Conflating them made the gate restart a healthy kubelet
  # because leftover pods from an earlier run held every GPU.
  phase="$(k get pod -n "$NS" "probe-$n" -o jsonpath='{.status.phase}' 2>/dev/null)"
  [ "$rc" = 1 ] && [ "$phase" = "Pending" ] && rc=2
  k delete pod -n "$NS" "probe-$n" --force --grace-period=0 >/dev/null 2>&1
  return $rc
}

# Which domain each node actually publishes — the probe has to ask for a device
# that exists there, and after the HA split not every node holds every domain.
node_domain() { # $1=node
  case "$1" in
    "$NODE_TSN_ONLY"|"$NODE_FAULT") echo "$DOMAIN_VPLC" ;;
    *) echo "$DOMAIN_INFER" ;;
  esac
}

# Every node that will host a claim gets probed, with a claim it can actually
# satisfy: a TSN stream on TSN nodes, a GPU everywhere else that has one. The
# GPU half matters because ai-training lands on GPU-only nodes, and a kubelet
# that was already running when a driver appeared wedges on its first claim
# (pod accepted, no sandbox requested, no event) until it is restarted.
prepare_health() {
  local n tmpl
  for n in $WORKERS; do
    if is_tsn_node "$n"; then tmpl="$(claim_name "$(node_domain "$n")")"
    elif is_gpu_node "$n" && [ "$WITH_GPU" = 1 ]; then tmpl="mixed-gpu"
    else continue
    fi
    prepare_probe "$n" "$tmpl" && { pass "prepare probe $n ($tmpl)"; continue; }
    if [ $? = 2 ]; then
      info "prepare probe $n ($tmpl): no free device — capacity, not a wedge. Run 'clean' first if this is unexpected."
      continue
    fi
    info "claim prepare wedged on $n — restarting kubelet"
    docker exec "$(node_container "$n")" systemctl restart kubelet
    sleep 12
    prepare_probe "$n" "$tmpl" || die "claim prepare still wedged on $n after kubelet restart"
    pass "prepare probe $n (after kubelet restart)"
  done
}

# --- HA stream layout -------------------------------------------------------
# lib.sh's ensure_streams hardcodes domainId cell-A on every node, which is what
# kind/deploy.sh installs: effectively k=M=3 for one domain. That is the layout
# fig_regime wanted (all TSN nodes interchangeable) and exactly the one this
# experiment must not have. fig_mixed pushes its own, so `streams` starts by
# wiping whatever deploy.sh left behind.
#
# Only TX streams are provisioned. The judgment is about which node can still
# originate a workload's traffic; RX (the node-level static receive point,
# ADR-0016) plays no part and would only add devices that the CEL selectors have
# to exclude anyway.
ACCS=(400000 100000 250000)      # per-node declared accumulated latency (ns), by
                                 # position in TSN_NODES. Three TSN nodes, three
                                 # entries — kind/deploy.sh carries six because it
                                 # indexes a longer list.

domains_of_node() { # $1=node -> the domains this node is provisioned for
  case "$1" in
    "$NODE_TSN_ONLY")  echo "$DOMAIN_VPLC" ;;
    "$NODE_FAULT")     echo "$DOMAIN_VPLC $DOMAIN_INFER" ;;
    "$NODE_SURV_GPU")  echo "$DOMAIN_INFER" ;;
    *)                 echo "" ;;
  esac
}
expected_devices() { # $1=node -> how many devices its pool should publish
  set -- $(domains_of_node "$1"); echo $#
}
stream_id() { # $1=domain $2=node -> stable, node-qualified CNC stream id
  echo "s-tx-$1-${2##*-}" | tr 'A-Z' 'a-z'
}
node_acc() { # $1=node -> declared accumulated latency, by position in the TSN list
  local i=0 n
  for n in $TSN_NODES; do
    [ "$n" = "$1" ] && { echo "${ACCS[$i]}"; return; }
    i=$((i+1))
  done
  echo 400000
}

# Caller must hold an open port-forward (cnc_pf_start).
push_node_streams() { # $1=node
  local node="$1" dom id nicmac acc
  nicmac="$(jget '{.spec.macAddress}' tsnnic "$node-eth0")"
  [ -n "$nicmac" ] || die "TSNNic $node-eth0 not found"
  acc="$(node_acc "$node")"
  for dom in $(domains_of_node "$node"); do
    id="$(stream_id "$dom" "$node")"
    info "push $id (talker=$node/$nicmac, domain=$dom, acc=${acc}ns)"
    cnc_curl POST /admin/streams "{\"streamId\":\"$id\",\"talker\":{\"mac\":\"$nicmac\"},\"listener\":{\"ip\":\"$ARM1_IP\",\"mac\":\"$ARM1_MAC\"},\"port\":4840,\"vlanId\":$TX_VLAN,\"pcp\":$TX_PCP,\"domainId\":\"$dom\",\"maxLatency\":$MAX_LATENCY_NS,\"accumulatedLatency\":$acc}" >/dev/null
  done
}

# lib.sh's device_count sums every slice whose POOL is named after the node, and
# a pool name is per-driver-unique but not globally unique: the GPU driver names
# its pool after the node too. Counting both made a TSN+GPU node read 2 TSN + 2
# GPU = 4 and the layout check never settled. Count our driver only.
tsn_device_count() { # $1=node
  k get resourceslices -o json 2>/dev/null | "$EXP_PY" -c '
import json,sys
d=json.load(sys.stdin)
print(sum(len(s["spec"].get("devices") or []) for s in d["items"]
          if s["spec"].get("driver")=="'"$DRIVER"'"
          and s["spec"].get("pool",{}).get("name")=="'"$1"'"))'
}

devices_settled() { # $1=node — the pool publishes exactly the layout, no more
  [ "$(tsn_device_count "$1")" -eq "$(expected_devices "$1")" ]
}

# Wipe every stream mock-cnc knows about and install the layout from scratch.
provision_streams() {
  local existing id n
  cnc_pf_start
  existing="$(cnc_curl GET /restconf/data/tsn-streams \
    | "$EXP_PY" -c 'import json,sys;print(" ".join(s["streamId"] for s in json.load(sys.stdin) or []))')"
  for id in $existing; do cnc_curl DELETE "/admin/streams/$id" >/dev/null; done
  [ -n "$existing" ] && info "cleared $(echo $existing | wc -w) pre-existing streams (deploy.sh defaults)"
  for n in $TSN_NODES; do push_node_streams "$n"; done
  cnc_pf_stop
  for n in $TSN_NODES; do
    # A wipe leaves stale devices until the controller's 30 s resync GCs the old
    # TSNStreams and the plugin's 3 s tick republishes; 60 s lost that race once.
    devices_settled "$n" || wait_for 150 devices_settled "$n" \
      || die "$n publishes $(tsn_device_count "$n") devices, expected $(expected_devices "$n")"
    pass "$n: $(tsn_device_count "$n") device(s) [$(domains_of_node "$n")]"
  done
}

# docker stop/start regenerates the kind node's eth0 MAC, so the CNC record stops
# matching the TSNNic and the controller GCs that node's TX TSNStreams. After
# every node restart the CNC copy must be replaced with the fresh MAC.
repair_streams() { # $1=node
  local node="$1" dom live_mac
  # the plugin's TSNNic update can lag node Ready; pushing a stale MAC would
  # leave the join broken, so wait until the CR matches the live NIC
  live_mac="$(docker exec "$(node_container "$node")" cat /sys/class/net/eth0/address 2>/dev/null || true)"
  if [ -n "$live_mac" ]; then
    nic_fresh() { [ "$(jget '{.spec.macAddress}' tsnnic "$node-eth0")" = "$live_mac" ]; }
    wait_for 90 nic_fresh || die "TSNNic $node-eth0 MAC not refreshed after restart"
  fi
  cnc_pf_start
  for dom in $(domains_of_node "$node"); do
    cnc_curl DELETE "/admin/streams/$(stream_id "$dom" "$node")" >/dev/null
  done
  push_node_streams "$node"
  cnc_pf_stop
  wait_for 150 devices_settled "$node" \
    || die "$node publishes $(tsn_device_count "$node") devices after repair, expected $(expected_devices "$node")"
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
    provision_streams
  else
    # Any node that does not match the layout exactly gets it re-installed. The
    # `-ne` (not `-lt`) matters: deploy.sh's cell-A-everywhere defaults show up as
    # too MANY devices, and leaving them would silently restore k=3.
    for n in $TSN_NODES; do
      devices_settled "$n" || { provision_streams; break; }
    done
  fi
  cat > "$OUT/topology.env" <<EOF
EXP_WORKERS=$((n_tsn + n_gen))
EXP_TSN_COUNT=$n_tsn
EXP_TSN_NODES="$TSN_NODES"
EXP_GEN_NODES="$GEN_NODES"
EXP_UTIL_TSN=$UTIL_TSN
EXP_UTIL_GEN=$UTIL_GEN
EXP_GPU_NODES="$GPU_NODES"
EXP_NODE_FAULT=$NODE_FAULT
EXP_NODE_SURV_GPU=$NODE_SURV_GPU
EXP_NODE_TSN_ONLY=$NODE_TSN_ONLY
EXP_DOMAIN_VPLC=$DOMAIN_VPLC
EXP_DOMAIN_INFER=$DOMAIN_INFER
EOF

  if resource_exists pod mock-vplc -n "$NS"; then
    info "deleting mock-vplc (holds devices)"
    k delete pod mock-vplc -n "$NS" --wait=true >/dev/null
  fi

  k apply -f manifests/priorityclasses.yaml >/dev/null

  # One TSN claim template per workload domain. The selector says "a TX stream in
  # MY domain" without naming a node, which is the whole mechanism: after the
  # fault only the surviving node still publishes such a device, so the Filter
  # resolves the placement that a label cannot.
  local dom
  for dom in "$DOMAIN_VPLC" "$DOMAIN_INFER"; do
    k apply -f - >/dev/null <<EOF
apiVersion: resource.k8s.io/v1
kind: ResourceClaimTemplate
metadata:
  name: $(claim_name "$dom")
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
                device.attributes['$DRIVER'].domainId == '$dom' &&
                device.attributes['$DRIVER'].direction == 'TX'
EOF
  done

  # GPU claim: the SECOND driver. Both arms use it — GPU is resourced either way,
  # which is what isolates the variable to "was TSN resourced?" (ADR-0039 §3).
  k apply -f - >/dev/null <<EOF
apiVersion: resource.k8s.io/v1
kind: ResourceClaimTemplate
metadata:
  name: mixed-gpu
  namespace: $NS
spec:
  spec:
    devices:
      requests:
      - name: gpu
        exactly:
          deviceClassName: $GPU_DRIVER
EOF

  [ -f "$OUT/mixed.csv" ] || echo "arm,cycle,workload,node0,on_tsn0,node1,on_tsn1,domain,stream_ok,detect_ms,evict_ms,sched_ms,ready_ms,resched_ms,pending" > "$OUT/mixed.csv"
  [ -f "$OUT/evictions.csv" ] || echo "arm,cycle,pod,priority_class,node,correct,ai_resched_ms" > "$OUT/evictions.csv"
  [ -f "$OUT/util.csv" ] || echo "arm,cycle,phase,pool,requests_mcpu,allocatable_mcpu,pct" > "$OUT/util.csv"
  prepare_health
  pass "setup done"
}

# --- manifests --------------------------------------------------------------
# The two guaranteed workloads share one generator because the ONLY thing that
# differs between them is which resources they require — which is exactly the
# variable the experiment isolates.
#
#   vplc-line     TSN        -> a cell-A claim (dratsn) or a label + env (vanilla)
#   ai-inference  TSN + GPU  -> the above, PLUS a GPU claim in BOTH arms
#
# Arm differences, and only these:
#
#   dratsn   schedulerName: tsn-scheduler, TSN stream held as a DRA claim.
#   vanilla  default scheduler, REQUIRED nodeAffinity on tsn.dra.net/capable
#            (a label can pin placement — that part works), plus the stream
#            parameters hardcoded as env. The env is a display ledger only:
#            kind runs -dataplane=false so no NRI/eBPF observes it. The judgment
#            is a control-plane fact (does the landing node still publish a live
#            TSNStream in this domain), same declared/measured split as the
#            bandwidth annotation below.
#
# No topologySpreadConstraints: fig_regime used a soft spread to pick its fault
# target, but here the initial placement is forced by fill order and must be
# identical every cycle (ADR-0039 §개정).
bw_of() { # $1=tier -> kubernetes.io/egress-bandwidth value. Display/ledger only:
          # the scheduler never reads it and kind's CNI does not shape on it.
  case "$1" in
    vplc-line) echo 5M ;; ai-inference) echo 150M ;;
    ai-training) echo 60M ;; logging) echo 10M ;; *) echo 1M ;;
  esac
}

guaranteed_manifest() { # $1=name $2=tier $3=mcpu $4=domain $5=with_gpu(0|1) $6=arm
  local name="$1" tier="$2" mcpu="$3" dom="$4" gpu="$5" arm="$6"
  cat <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: $name
  namespace: $NS
  labels: {exp: fig-mixed}
spec:
  replicas: 1
  selector:
    matchLabels: {app: $name}
  template:
    metadata:
      labels: {app: $name, exp: fig-mixed, tier: $tier, mixed-group: guaranteed}
      annotations:
        kubernetes.io/egress-bandwidth: "$(bw_of "$tier")"
        tsn.dra.net/domain: "$dom"
    spec:
$([ "$arm" = dratsn ] && echo "      schedulerName: tsn-scheduler")
      priorityClassName: $tier
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
$([ "$arm" = vanilla ] && cat <<AFF
      affinity:
        nodeAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            nodeSelectorTerms:
            - matchExpressions:
              - key: tsn.dra.net/capable
                operator: In
                values: ["true"]
AFF
)
      containers:
      - name: app
        image: busybox:1.36
        command: ["sh","-c","sleep 3600"]
$([ "$arm" = vanilla ] && cat <<ENV
        env:
        - {name: TSN_STREAMS, value: "$(stream_id "$dom" "$NODE_FAULT"):vlan=$TX_VLAN,pcp=$TX_PCP"}
ENV
)
        resources:
          requests: {cpu: "${mcpu}m", memory: 512Mi}
          limits:   {cpu: "${mcpu}m", memory: 512Mi}
          claims:
$([ "$arm" = dratsn ] && echo "          - {name: tsn}")
$([ "$gpu" = 1 ] && echo "          - {name: gpu}")
      resourceClaims:
$([ "$arm" = dratsn ] && cat <<TSNCLAIM
      - name: tsn
        resourceClaimTemplateName: $(claim_name "$dom")
TSNCLAIM
)
$([ "$gpu" = 1 ] && cat <<GPUCLAIM
      - name: gpu
        resourceClaimTemplateName: mixed-gpu
GPUCLAIM
)
EOF
}

# Fillers: one Deployment per node with *preferred* affinity to it. Preferred,
# not required: the initial fill lands where the plan says (the target node is
# the only one with room at its own weight), while a replacement created after
# preemption is free to move — evicted AI must be able to migrate to a general
# node, which is the whole story. cpu-only requests: the puzzle is N x C
# (ADR-0035 excludes the storage axis, not memory — memory simply never binds
# here, every pod asks for 128-512Mi against 121GiB of host RAM).
filler_manifest() { # $1=name $2=priorityclass $3=node $4=replicas $5=mcpu $6=with_gpu(0|1)
  local gpu="${6:-0}"
  cat <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: $1
  namespace: $NS
  labels: {exp: fig-mixed}
spec:
  replicas: $4
  selector:
    matchLabels: {app: $1}
  template:
    metadata:
      labels: {app: $1, exp: fig-mixed, tier: $2}
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
$([ "$gpu" = 1 ] && cat <<GPUFILL
          claims: [{name: gpu}]
      resourceClaims:
      - name: gpu
        resourceClaimTemplateName: mixed-gpu
GPUFILL
)
EOF
}

exp_pods_json() { k get pods -n "$NS" -l exp=fig-mixed -o json 2>/dev/null; }
# settled = every experiment Deployment reports readyReplicas == replicas.
# Checking pod phases instead races the ReplicaSets: the check can pass while
# half the replicas are not created yet, which let the AI plan run before the
# vPLCs were even bound (observed in the gate run).
all_running() {
  k get deploy -n "$NS" -l exp=fig-mixed -o json 2>/dev/null | "$EXP_PY" -c '
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

# fill <arm>: force the initial placement instead of letting a spread constraint
# choose it. Every cycle must start from the identical state — the procedure is
# what is reproducible, not the outcome (ADR-0039 §개정).
#
#   w2   vplc-line only                    <- dies
#   w3   ai-inference + elastic, ~90%      <- survives, does NOT move
#   w1   elastic, ~90%                     <- survives
#   gen  elastic, ~90% minus absorb headroom
#
# TWO PROPERTIES ARE LOAD-BEARING, and an earlier design lost the experiment by
# getting them wrong. Recorded here because both failures were silent.
#
#   THE SURVIVORS MUST BE INTERCHANGEABLE. After the fault, vplc-line's only
#   candidates are w1 and w3, and vanilla cannot tell them apart by label — so
#   the cluster must not tell them apart by anything else either. They are filled
#   to the same target with the same unit, so free CPU, victim priority and
#   victim count all match, and the scheduler is left with a real tie that its
#   randomised tie-break resolves.
#
#   ai-inference MUST NOT BE DISPLACED BY THIS FAULT. When it started on the
#   dying node it was forced onto w3, consumed capacity there, and made w1 the
#   strictly better node for vplc-line — vanilla then landed correctly 10/10 and
#   the arm ceased to exist. Its placement still demonstrates GPU-as-a-resource:
#   it is admitted only to the TSN+GPU pair, in both arms. It just demonstrates
#   that at placement time rather than at failover.
#
# Preemption IS required here, and that is deliberate: the ~90% regime is what
# makes the elastic/guaranteed axis visible (something has to be pushed), and it
# is the same regime fig_regime measured, so the two experiments stay comparable.
STAGING_TAINT="fig-mixed.dra.net/staging"

# The DRA prepare wedge is not a startup-only condition: a kubelet that has been
# up for a while will accept a claim pod, bind it, and then never request a
# sandbox — no event, no error, just ContainerCreating forever. It recurs, and a
# long demo loop cannot die on it. Restarting the kubelet on the affected node
# clears it; this heals in place instead of failing the cycle.
heal_wedged_nodes() {
  local stuck n healed=0
  stuck="$(exp_pods_json | "$EXP_PY" -c '
import json,sys
seen=set()
for p in json.load(sys.stdin).get("items", []):
    if p["status"].get("phase") in ("Running","Succeeded"): continue
    n = p["spec"].get("nodeName")
    if n and n not in seen:      # bound but not running = wedge, not capacity
        seen.add(n); print(n)')"
  for n in $stuck; do
    info "claim prepare wedged on $n — restarting kubelet"
    docker exec "$(node_container "$n")" systemctl restart kubelet && healed=1
  done
  [ "$healed" = 1 ] && sleep 15
  [ "$healed" = 1 ]
}

# wait for the fixture to settle, healing a wedge once before giving up
settle() { # $1=timeout $2=what
  wait_for "$1" all_running && return 0
  heal_wedged_nodes || die "$2 did not settle ($(count_running) running) and no wedged node found"
  wait_for "$1" all_running || die "$2 did not settle after healing ($(count_running) running)"
}

stage_only() { # taint every TSN node except $1, so a pod can only land there
  local keep="$1" n
  for n in $TSN_NODES; do
    if [ "$n" = "$keep" ]; then k taint node "$n" "$STAGING_TAINT-" >/dev/null 2>&1
    else k taint node "$n" "$STAGING_TAINT=true:NoSchedule" --overwrite >/dev/null
    fi
  done
}
stage_clear() { local n; for n in $TSN_NODES; do k taint node "$n" "$STAGING_TAINT-" >/dev/null 2>&1; done; }

fill() {
  local arm="$1" node count trim
  case "$arm" in vanilla|dratsn) ;; *) die "unknown arm '$arm' (want: vanilla dratsn)" ;; esac

  # 1. ai-inference onto the SURVIVING TSN+GPU node
  stage_only "$NODE_SURV_GPU"
  guaranteed_manifest ai-inference ai-inference "$INFER_MCPU" "$DOMAIN_INFER" "$WITH_GPU" "$arm" | k apply -f - >/dev/null
  settle 180 "ai-inference did not settle ($(count_running) running)"

  # 2. vplc-line onto the node that will die
  stage_only "$NODE_FAULT"
  guaranteed_manifest vplc-line vplc-line "$VPLC_MCPU" "$DOMAIN_VPLC" 0 "$arm" | k apply -f - >/dev/null
  settle 180 "vplc-line did not settle"
  stage_clear

  local vn in_
  vn="$(k get pods -n "$NS" -l app=vplc-line -o jsonpath='{.items[0].spec.nodeName}')"
  in_="$(k get pods -n "$NS" -l app=ai-inference -o jsonpath='{.items[0].spec.nodeName}')"
  [ "$vn" = "$NODE_FAULT" ] || die "vplc-line landed on $vn, expected $NODE_FAULT"
  [ "$in_" = "$NODE_SURV_GPU" ] || die "ai-inference landed on $in_, expected $NODE_SURV_GPU"
  pass "initial placement pinned: vplc-line on $NODE_FAULT, ai-inference on $NODE_SURV_GPU"

  # 3. one ai-training on the surviving TSN+GPU node, so its second GPU is used
  #    and the elastic tier is present on a node that will have to give ground
  filler_manifest "train-$NODE_SURV_GPU" ai-training "$NODE_SURV_GPU" 1 "$TRAIN_MCPU" "$WITH_GPU" | k apply -f - >/dev/null
  settle 120 "ai-training on $NODE_SURV_GPU did not settle"

  # 4. fill ALL THREE TSN nodes to UTIL_TSN with the same unit. No reserve: the
  #    displaced vplc-line must have to preempt, on either survivor, equally.
  #
  #    The dying node is filled too. Leaving it nearly empty was an artefact —
  #    it put the TSN pool at 66% and broke the comparison with fig_regime's 90%,
  #    and it also made the fault unrealistically cheap. Its elastic work has
  #    nowhere to go once the survivors are full, so those pods go Pending: that
  #    is reported as cost, never judged (ADR-0039 §4). A dead node's load not
  #    fitting anywhere is the honest outcome of a tight regime.
  $WF plan --nodes "$NODE_TSN_ONLY $NODE_SURV_GPU $NODE_FAULT" --util "$UTIL_TSN" \
      --unit-mcpu "$LOG_MCPU" | while read -r node count trim; do
    [ "$count" -gt 0 ] && filler_manifest "log-$node" logging "$node" "$count" "$LOG_MCPU" 0 | k apply -f - >/dev/null
    [ "$trim" -gt 0 ]  && filler_manifest "log-$node-trim" logging "$node" 1 "$trim" 0 | k apply -f - >/dev/null
  done
  settle 180 "TSN fill did not settle"

  # 5. general nodes: ai-training where there are GPUs, logging everywhere, and
  #    headroom to absorb what preemption pushes off the TSN pool.
  for node in $GEN_NODES; do
    is_gpu_node "$node" && { filler_manifest "train-$node" ai-training "$node" 2 "$TRAIN_MCPU" "$WITH_GPU" | k apply -f - >/dev/null; }
  done
  settle 120 "ai-training on general nodes did not settle"

  local absorb=$(( (VPLC_MCPU + LOG_MCPU + n_gen - 1) / n_gen ))
  $WF plan --nodes "$GEN_NODES" --util "$UTIL_GEN" --unit-mcpu "$LOG_MCPU" \
      --reserve-mcpu "$absorb" | while read -r node count trim; do
    [ "$count" -gt 0 ] && filler_manifest "log-$node" logging "$node" "$count" "$LOG_MCPU" 0 | k apply -f - >/dev/null
    [ "$trim" -gt 0 ]  && filler_manifest "log-$node-trim" logging "$node" 1 "$trim" 0 | k apply -f - >/dev/null
  done
  settle 180 "general fill did not settle"

  # The symmetry the whole vanilla arm rests on. Warn rather than die: a small
  # skew is reported with the result (ADR-0039 §한계) instead of silently biasing
  # it, and the verdict's degeneracy check is the backstop.
  local free_a free_b
  free_a="$($WF free --node "$NODE_TSN_ONLY")"
  free_b="$($WF free --node "$NODE_SURV_GPU")"
  if [ "$free_a" = "$free_b" ]; then
    pass "survivor headroom equal: $NODE_TSN_ONLY=$free_a $NODE_SURV_GPU=$free_b mcpu (both < one vPLC: preemption required)"
  else
    info "WARNING: survivor headroom differs ($NODE_TSN_ONLY=$free_a $NODE_SURV_GPU=$free_b mcpu) — vanilla's tie is skewed"
  fi
  [ "$free_a" -lt "$VPLC_MCPU" ] || info "WARNING: $NODE_TSN_ONLY has room for a vPLC without preemption — the regime is loose"
  pass "fill($arm): $(count_running) pods running"
}

# The node hosting vplc-line is the target. ai-inference deliberately lives
# elsewhere: when it shared the dying node it was forced onto the same survivor
# vplc-line was choosing between, consumed capacity there, and turned the choice
# into a foregone conclusion (vanilla scored 10/10 and stopped being an arm).
# Read from the live cluster rather than assumed, so the driver still works if
# the fill landed somewhere unexpected — but if the fill did its job this is
# always NODE_FAULT, and inject_fault warns when it is not.
fault_target() {
  exp_pods_json | "$EXP_PY" -c '
import json,sys
for p in json.load(sys.stdin).get("items", []):
    l = p["metadata"].get("labels", {})
    if l.get("tier") == "vplc-line" and p["spec"].get("nodeName") \
       and not p["metadata"].get("deletionTimestamp"):
        print(p["spec"]["nodeName"]); break
else: print("")'
}

inject_fault() {
  local node0 t0
  node0="$(fault_target)"
  [ -n "$node0" ] || die "no node hosts vplc-line — run 'fill <arm>' first"
  [ "$node0" = "$NODE_FAULT" ] || info "WARNING: vplc-line is on $node0, expected $NODE_FAULT"
  info "kill $node0 (vplc-line host)"
  t0=$(now_ms)
  # -t 0: SIGKILL immediately instead of the default 10 s SIGTERM grace. With the
  # grace period the node keeps renewing its lease for part of the shutdown, so
  # t0 sits several seconds before the node actually goes silent and detect_ms is
  # inflated by that much (measured 10.9-12.4 s against a model max of 10 s).
  # Instant death is also the more honest model of a node fault.
  docker stop -t 0 "$(node_container "$node0")" >/dev/null
  cat > "$STATE" <<EOF
NODE0=$node0
T0=$t0
EOF
}

node_is_ready() {
  [ "$(k get node "$1" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)" = "True" ]
}

record() { # $1=arm $2=cycle
  local arm="$1" trial="$2"
  . "$STATE"
  $WF watch --arm "$arm" --trial "$trial" --node0 "$NODE0" --t0-ms "$T0" \
      --tsn-nodes "$TSN_NODES" --ns "$NS" --out "$OUT" --timeout "$WATCH_TIMEOUT" \
      --domains "vplc-line=$DOMAIN_VPLC,ai-inference=$DOMAIN_INFER"
  $WF util --arm "$arm" --trial "$trial" --phase post --tsn-nodes "$TSN_NODES" --csv "$OUT/util.csv"

  docker start "$(node_container "$NODE0")" >/dev/null
  wait_for 120 node_is_ready "$NODE0" || die "$NODE0 did not come back Ready"
  # the restart regenerated the node's MAC; put its CNC streams back in join
  repair_streams "$NODE0"
}

clean() {
  k delete deployment -n "$NS" -l exp=fig-mixed --wait=true >/dev/null 2>&1
  # pods that died with the node come back Terminating when the kubelet
  # restarts; they hold their ResourceClaims (and so TX devices) until removed,
  # which would starve the next trial's allocation — force them out.
  k delete pods -n "$NS" -l exp=fig-mixed --force --grace-period=0 >/dev/null 2>&1
  wait_for 120 claims_gone || die "ResourceClaims still present after 120s — the next cycle would under-allocate"
  # the staging taint is normally lifted by fill(); clear it here too so an
  # aborted fill cannot leave a TSN node unschedulable for the next cycle
  for n in $TSN_NODES; do k taint node "$n" "$STAGING_TAINT-" >/dev/null 2>&1; done
}

restore_all() {
  for n in $TSN_NODES; do
    c="$(node_container "$n")"
    [ "$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null)" = "true" ] || docker start "$c" >/dev/null 2>&1
    k taint node "$n" "$STAGING_TAINT-" >/dev/null 2>&1
  done
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

# Checks the pre-registered predictions of ADR-0039 §4, written down before any
# collection. P2 is deliberately a wide band, not an equality: vanilla's result
# is a coin, and a run of 10 that happened to come out 8/10 is not a bug.
verdict() {
  "$EXP_PY" - "$OUT" <<'PY'
import csv, sys, collections
out = sys.argv[1]
rows = list(csv.DictReader(open(f"{out}/mixed.csv")))
ev   = list(csv.DictReader(open(f"{out}/evictions.csv")))
util = list(csv.DictReader(open(f"{out}/util.csv")))

def sel(arm, wl):
    return [r for r in rows if r["arm"] == arm and r["workload"] == wl]
def fail(m): print(f"VERDICT-FAIL {m}")
def ok(m):   print(f"VERDICT-PASS {m}")
def rate(rs):
    n = len(rs)
    return sum(1 for r in rs if r["stream_ok"] == "1"), n

# P1 dratsn always finds the surviving path.
d = sel("dratsn", "vplc-line")
if d:
    good, n = rate(d)
    (ok if good == n else fail)(f"P1 dratsn vplc-line: {good}/{n} landed on a node with a live cell stream")

# P2 vanilla is a coin. The claim is the DISTRIBUTION, so the check is a band
# around (k-1)/(M-1)=0.5 and a hard failure only if it is perfect either way —
# 0/n or n/n would mean the tie is not a tie and the fill skewed it.
v = sel("vanilla", "vplc-line")
if v:
    good, n = rate(v)
    if n >= 4 and good in (0, n):
        fail(f"P2 vanilla vplc-line: {good}/{n} — degenerate, the survivors are not interchangeable "
             f"(check the equal-headroom warning from fill)")
    else:
        ok(f"P2 vanilla vplc-line: {good}/{n} correct (expected ~{n/2:.0f}/{n}, this is a coin)")

# P3 the control: ai-inference is admitted only to the TSN+GPU pair, in BOTH
# arms, because GPU is a modelled resource either way. It does not share the
# dying node, so it is never displaced and never appears here — its placement is
# asserted at fill time, which dies on a mismatch. Seeing it in these rows means
# the fault hit the wrong node and the run is invalid.
moved = [r for r in rows if r["workload"] == "ai-inference"]
(ok if not moved else fail)(
    "P3 ai-inference was not displaced by the fault "
    f"({'as designed' if not moved else str(len(moved)) + ' rows — fault hit the wrong node'})")

# Both arms must actually recover; a pending guaranteed workload is a failure of
# its own kind (ADR-0039 §4) and would invalidate the stream_ok reading above.
pend = [r for r in rows if r["pending"] != "0"]
(ok if not pend else fail)(f"no guaranteed workload left Pending ({len(pend)} pending rows)")

# P4 elastic absorbs the displacement. Reported, never judged: whether a delayed
# training job counts as damage is the shop floor's call, not the paper's.
tiers = collections.Counter(e["priority_class"] for e in ev)
guar = {t: c for t, c in tiers.items() if t in ("vplc-line", "ai-inference")}
(ok if not guar else fail)(f"P4 only elastic workloads displaced (guaranteed displaced: {guar or 'none'})")
if tiers:
    print("displaced by tier: " + ", ".join(f"{t}={c}" for t, c in sorted(tiers.items())))

# P5 detection is the same constant for both arms — stated so that no reader
# mistakes a per-arm recovery number for a per-arm claim.
det = collections.defaultdict(list)
for r in rows:
    if r["detect_ms"] != "-1":
        det[r["arm"]].append(int(r["detect_ms"]))
for arm, xs in sorted(det.items()):
    xs.sort()
    print(f"detect {arm}: median {xs[len(xs)//2]/1000:.1f}s over {len(xs)} samples "
          f"[{min(xs)/1000:.1f}-{max(xs)/1000:.1f}s]")

def mean_pre(a, pool):
    xs = [float(u["pct"]) for u in util
          if u["arm"] == a and u["phase"] == "pre" and u["pool"] == pool]
    return sum(xs) / len(xs) if xs else 0.0
for a in sorted({r["arm"] for r in rows}):
    print(f"pre-fault util {a}: tsn {mean_pre(a,'tsn'):.1f}% general {mean_pre(a,'general'):.1f}%")
print(f"rows: mixed={len(rows)} evictions={len(ev)}")
PY
}

# --- dispatch ---------------------------------------------------------------
case "${1:-all}" in
  setup)        setup ;;
  streams)      provision_streams ;;
  fill)         setup >/dev/null; fill "${2:?fill <arm>}" ;;
  inject-fault) inject_fault ;;
  repair)       shift; for n in "${@:?repair <node>...}"; do repair_streams "$n"; done ;;
  record)       record "${2:?record <arm> <cycle>}" "${3:?record <arm> <cycle>}" ;;
  clean)        clean ;;
  all)
    trap restore_all EXIT
    # leftovers from a staged/manual session would hold devices and make the
    # health gate read "wedged" when the cluster is merely full
    clean
    setup
    for arm in $ARMS; do
      hdr "arm $arm"
      for i in $(seq 1 "$MIXED_CYCLES"); do
        run_trial "$arm" "$i"
        info "$arm $i/$MIXED_CYCLES -> $(tail -1 "$OUT/mixed.csv" | cut -d, -f3,6,8,9)"
      done
    done
    k delete resourceclaimtemplate -n "$NS" "$(claim_name "$DOMAIN_VPLC")" "$(claim_name "$DOMAIN_INFER")" mixed-gpu >/dev/null 2>&1
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
  *) die "usage: $0 [setup|streams|fill <arm>|inject-fault|repair <node>...|record <arm> <cycle>|clean|all]" ;;
esac
