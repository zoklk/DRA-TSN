#!/usr/bin/env bash
# DRATSN build-test loop driver. Builds static binaries, images, pushes to the
# in-cluster registry, deploys, drives a CNC stream + claim, runs smoke tests.
#
# Usage:
#   ./loop.sh build      # build binaries + images + push
#   ./loop.sh deploy     # apply all manifests (registry/certs assumed up)
#   ./loop.sh scenario   # push a CNC stream + apply example claim/pod
#   ./loop.sh test       # run smoke tests (phases 1,2 here; 3 via debug pod)
#   ./loop.sh all        # build -> deploy -> scenario -> test
#   ./loop.sh redeploy   # build + rollout restart driver/controller/mock-cnc
#   ./loop.sh clearpins  # remove stale bpf pins on ALL dratsn nodes (after bpf change); NODES="..." to scope
#   ./loop.sh e2e        # apply dual TX+RX claim pod; prints the dataplane_e2e.sh cmd
set -uo pipefail
cd "$(dirname "$0")/.."

REG="${REG:-192.168.110.11:30500}"
PUSH_REG="${PUSH_REG:-localhost:30500}"   # docker pushes here (localhost = insecure ok)
TAG="${TAG:-dev}"
TSN_NODE="${TSN_NODE:-alpha-w1}"
TSN_IFACE="${TSN_IFACE:-eno1}"
RX_ENDPOINT_IP="${RX_ENDPOINT_IP:-192.168.1.1}"
NS="${NS:-dratsn-test}"
export GOTOOLCHAIN=auto

say() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }

build() {
  say "build binaries"
  mkdir -p bin
  for c in controller mock-cnc kubeletplugin; do
    CGO_ENABLED=0 go build -o "bin/$c" "./cmd/$c" || return 1
  done
  say "build + push images"
  declare -A MAP=( [controller]=dratsn-controller [mock-cnc]=dratsn-mock-cnc [kubeletplugin]=dratsn-driver )
  for c in "${!MAP[@]}"; do
    img="$PUSH_REG/${MAP[$c]}:$TAG"
    docker build -q -f "deployments/Dockerfile.$c" -t "$img" . >/dev/null || return 1
    docker push -q "$img" >/dev/null || return 1
    echo "  pushed ${MAP[$c]}:$TAG"
  done
}

deploy() {
  say "apply CRDs + deviceclass"
  kubectl apply -f deployments/crds/ >/dev/null
  kubectl apply -f deployments/deviceclass.yaml >/dev/null
  say "apply registry + certs"
  kubectl apply -f deployments/registry.yaml -f deployments/registry-certs.yaml >/dev/null
  # DaemonSet runs on ALL nodes (no nodeSelector). A node is a TSN node iff it
  # carries the capable-nics label; here we declare exactly one (TSN_NODE/eno1) so
  # the scheduler can only place vplc pods there (correct vs incorrect placement).
  say "declare TSN node $TSN_NODE iface $TSN_IFACE (capable-nics label)"
  kubectl label node "$TSN_NODE" "tsn.dra.net/capable-nics=$TSN_IFACE" --overwrite >/dev/null
  say "apply mock-cnc + controller + daemonset"
  for f in mock-cnc controller daemonset; do
    sed -e "s#REGISTRY#$REG#g" -e "s#:TAG#:$TAG#g" "deployments/$f.yaml" | kubectl apply -f - >/dev/null
  done
}

redeploy() {
  build || return 1
  say "rollout restart"
  kubectl -n kube-system rollout restart deploy/mock-cnc deploy/tsn-controller ds/dratsn >/dev/null 2>&1
  kubectl -n kube-system rollout status ds/dratsn --timeout=120s | tail -1
}

# push a CNC stream via the mock admin API (TX matching the TSN NIC), apply claim.
# mock-cnc is a distroless image (no shell), so drive it via port-forward + curl.
scenario() {
  kubectl create ns "$NS" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  local mac
  mac=$(kubectl get tsnnic "${TSN_NODE}-${TSN_IFACE}" -o jsonpath='{.spec.macAddress}' 2>/dev/null)
  say "mock CNC: push 2 robot arms in cell-A (arm-1 .11 / arm-2 .12) TX+RX, listener ip=$RX_ENDPOINT_IP"
  kubectl -n kube-system port-forward svc/mock-cnc 18080:8080 >/tmp/dratsn-pf.log 2>&1 &
  local pf=$!; sleep 2
  # Two robot arms, SAME cell-A => shared VLAN/PCP (TX 200/6, RX 100/5); arms differ
  # only by IP+MAC. RX node-endpoint ports split (arm-1=4840, arm-2=4841) so each
  # arm's echo DNATs to its own pod socket. TX(egress)=200, RX(ingress)=100 — the
  # physical switch carries the 100-range.
  # arm-1 (.11, mac 0c:c4:7a:ca:3b:f2 = beta-w2, swapped from old box 0c:c4:7a:c4:a7:2a) — s-tx / s-rx
  curl -s -XPOST localhost:18080/admin/streams \
    -d "{\"streamId\":\"s-tx\",\"talker\":{\"mac\":\"$mac\"},\"listener\":{\"ip\":\"192.168.1.11\",\"mac\":\"0c:c4:7a:ca:3b:f2\"},\"port\":4840,\"vlanId\":200,\"pcp\":6,\"domainId\":\"cell-A\",\"maxLatency\":1000000,\"accumulatedLatency\":250000}" >/dev/null
  curl -s -XPOST localhost:18080/admin/streams \
    -d "{\"streamId\":\"s-rx\",\"talker\":{\"ip\":\"192.168.1.11\"},\"listener\":{\"ip\":\"$RX_ENDPOINT_IP\"},\"port\":4840,\"vlanId\":100,\"pcp\":5,\"domainId\":\"cell-A\",\"maxLatency\":1000000,\"accumulatedLatency\":250000}" >/dev/null
  # arm-2 (.12, mac 0c:c4:7a:ca:3c:02 = beta-w3, swapped from old box 0c:c4:7a:ca:3c:3a) — s-tx2 / s-rx2; RX port 4841
  curl -s -XPOST localhost:18080/admin/streams \
    -d "{\"streamId\":\"s-tx2\",\"talker\":{\"mac\":\"$mac\"},\"listener\":{\"ip\":\"192.168.1.12\",\"mac\":\"0c:c4:7a:ca:3c:02\"},\"port\":4840,\"vlanId\":200,\"pcp\":6,\"domainId\":\"cell-A\",\"maxLatency\":1000000,\"accumulatedLatency\":250000}" >/dev/null
  curl -s -XPOST localhost:18080/admin/streams \
    -d "{\"streamId\":\"s-rx2\",\"talker\":{\"ip\":\"192.168.1.12\"},\"listener\":{\"ip\":\"$RX_ENDPOINT_IP\"},\"port\":4841,\"vlanId\":100,\"pcp\":5,\"domainId\":\"cell-A\",\"maxLatency\":1000000,\"accumulatedLatency\":250000}" >/dev/null
  kill "$pf" 2>/dev/null
  sleep 4
  say "apply example claim + vplc pod"
  kubectl apply -f deployments/examples/vplc-tx.yaml >/dev/null
}

test_all() {
  say "smoke tests (phase 1,2 from here; phase 3 via debug pod on $TSN_NODE)"
  NS="$NS" TSN_IFACE="$TSN_IFACE" RX_ENDPOINT_IP="$RX_ENDPOINT_IP" PHASES="1 2" ./tests/run_all.sh
  NS="$NS" TSN_IFACE="$TSN_IFACE" NODE="$TSN_NODE" RX_ENDPOINT_IP="$RX_ENDPOINT_IP" bash ./tests/smoke_phase3_dataplane.sh
}

# clearpins: remove stale pinned maps/links. REQUIRED after a bpf map struct change
# (e.g. tx_flows ValueSize 8->24 → new pod fails: "map spec is incompatible") and
# after a program change so pinned links re-attach the new prog. Map entries are
# rebuilt from the checkpoint by NRI Synchronize after restart.
# The DaemonSet runs on ALL nodes, so every node that loaded the driver has pins.
# Default: clear EVERY node running the dratsn DS (run once from m1). Override with
# NODES="alpha-w1 alpha-w2" ./loop.sh clearpins.
clearpins() {
  local nodes="${NODES:-}"
  if [ -z "$nodes" ]; then
    nodes=$(kubectl -n kube-system get pods -l app=dratsn \
      -o jsonpath='{range .items[*]}{.spec.nodeName}{"\n"}{end}' 2>/dev/null | sort -u | tr '\n' ' ')
  fi
  say "clear stale bpf pins on: ${nodes:-<none>}"
  for n in $nodes; do
    printf '  -- %s: ' "$n"
    kubectl debug "node/$n" -it --image=busybox:1.36 --profile=sysadmin -- \
      chroot /host sh -c 'rm -rf /sys/fs/bpf/tsn && echo cleared' </dev/null 2>/dev/null | grep -v 'Creating debugging pod'
  done
  kubectl get pods -A 2>/dev/null | grep node-debugger | awk '{print $1,$2}' | \
    while read -r ns nm; do kubectl delete pod -n "$ns" "$nm" --wait=false >/dev/null 2>&1; done
}

# e2e: apply the dual TX+RX claim pod, then point the user at dataplane_e2e.sh
# (which must run ON the worker node as root: bpftool + tcpdump + python3).
e2e() {
  kubectl create ns "$NS" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  say "apply mock-vplc (claims TX+RX for both cell-A arms: s-tx/s-rx + s-tx2/s-rx2)"
  kubectl apply -f deployments/examples/mock-vplc.yaml >/dev/null
  kubectl -n "$NS" wait --for=condition=Ready pod/mock-vplc --timeout=60s 2>/dev/null || true
  say "run the transform checks ON $TSN_NODE as root:"
  echo "  sudo NS=$NS TSN_IFACE=$TSN_IFACE RX_ENDPOINT_IP=$RX_ENDPOINT_IP ./tests/dataplane_e2e.sh all"
}

case "${1:-all}" in
  build) build ;;
  deploy) deploy ;;
  redeploy) redeploy ;;
  clearpins) clearpins ;;
  scenario) scenario ;;
  e2e) e2e ;;
  test) test_all ;;
  all) build && deploy && scenario && test_all ;;
  *) echo "unknown: $1"; exit 1 ;;
esac
