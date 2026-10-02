#!/usr/bin/env bash
# Deploy DRATSN onto the kind cluster in control-plane-only mode
# (-dataplane=false: no eBPF, no NRI — no TSN NIC required).
#
# Steps: build images -> kind load -> CRDs/deviceclass/mock-cnc/controller/
# daemonset -> label one worker as TSN-capable (eth0) -> push the 4 canonical
# cell-A streams to mock-cnc -> wait for ResourceSlice devices.
set -eu
cd "$(dirname "$0")"
REPO="$(cd ../.. && pwd)"
REG=dratsn.kind TAG=kind
: "${KIND_CLUSTER:=dratsn}"       # matches create-cluster.sh
KNODE="$KIND_CLUSTER-worker1"     # plays alpha-w1 (container: <cluster>-worker)
export KUBECTL="kubectl"
# Pin this run to a per-cluster kubeconfig instead of switching the shared
# current-context. `kind create cluster` rewrites that context as a side effect,
# so a concurrent create for another cluster used to silently redirect every
# kubectl here at the wrong cluster (observed: "nodes dratsn-worker1 not found"
# while dratsn-worker1 was Ready). Same convention run.sh / live_serve.py already
# follow. KUBECONFIG rather than `--context` because lib.sh runs `exec "$KUBECTL"`,
# which treats the variable as a single word.
KUBECONFIG_FILE="$HOME/.kube/kind-$KIND_CLUSTER.kubeconfig"
kind export kubeconfig --name "$KIND_CLUSTER" --kubeconfig "$KUBECONFIG_FILE" >/dev/null
export KUBECONFIG="$KUBECONFIG_FILE"

# experiment helpers against the kind topology
export TSN_NODE="$KNODE" TSN_IFACE=eth0
. ../lib.sh

hdr "build + load images"
make -C "$REPO" build image REGISTRY=$REG TAG=$TAG
kind load docker-image --name "$KIND_CLUSTER" \
  $REG/dratsn-controller:$TAG $REG/dratsn-mock-cnc:$TAG $REG/dratsn-driver:$TAG \
  $REG/dratsn-tsn-scheduler:$TAG

hdr "apply manifests (dataplane=false, imagePullPolicy=Never)"
k apply -f "$REPO/deployments/crds/" >/dev/null
k apply -f "$REPO/deployments/deviceclass.yaml" >/dev/null
for f in mock-cnc controller daemonset tsn-scheduler; do
  sed -e "s#REGISTRY#$REG#g" -e "s#:TAG#:$TAG#g" \
      -e "s#imagePullPolicy: Always#imagePullPolicy: Never#g" \
      -e "s#-dataplane=true#-dataplane=false#g" \
      "$REPO/deployments/$f.yaml" | k apply -f - >/dev/null
done

# More than one TSN node so a fault has somewhere to reschedule to (fig_twin);
# the rest of the pool stays non-TSN so a blind scheduler visibly scatters.
: "${KIND_TSN_NODES:=$KIND_CLUSTER-worker1 $KIND_CLUSTER-worker2 $KIND_CLUSTER-worker3}"
SFX=("" "-b" "-c" "-d" "-e" "-f")
# Per-node CNC accumulated-latency (ns): differentiates slack for the
# tsn-scheduler score. slack = MAX_LATENCY_NS(1ms) - ACC, so worker2 (index 1)
# is the best-slack winner a lone slack-scheduled pod must land on.
ACC=(400000 100000 250000 400000 400000 400000)

hdr "label TSN-capable nodes (eth0): $KIND_TSN_NODES"
for n in $KIND_TSN_NODES; do
  k label node "$n" tsn.dra.net/capable=true tsn.dra.net/capable-nics=eth0 --overwrite >/dev/null
done

wait_for 120 deploy_available tsn-controller kube-system || die "controller not up"
wait_for 120 ds_ready dratsn kube-system || die "daemonset not up"
wait_for 120 deploy_available tsn-scheduler kube-system || die "tsn-scheduler not up"

# streams: talker MAC = each node's eth0 MAC, RX listener = that node's host IP.
# The domainId stays cell-A on every node, so one ResourceClaim CEL selector
# matches devices across all of them — that is what makes them interchangeable
# placement targets.
cnc_pf_start
i=0
for n in $KIND_TSN_NODES; do
  nic_ready() { resource_exists tsnnic "$n-eth0"; }
  wait_for 60 nic_ready || die "TSNNic $n-eth0 not discovered"
  rx_ip="$(jget '{.spec.hostIP}' tsnnic "$n-eth0")"
  hdr "push cell-A streams (talker=$n eth0, rx endpoint=$rx_ip, suffix='${SFX[$i]}', acc=${ACC[$i]}ns)"
  ensure_streams "$n" eth0 "${SFX[$i]}" "$rx_ip" "${ACC[$i]}"
  i=$((i+1))
done
cnc_pf_stop
export RX_ENDPOINT_IP="$(jget '{.spec.hostIP}' tsnnic "$KNODE-eth0")"

for n in $KIND_TSN_NODES; do
  node_devices_up() { [ "$(device_count "$n")" -ge 4 ]; }
  wait_for 60 node_devices_up || die "expected 4 devices on $n, got $(device_count "$n")"
  pass "$n: $(device_count "$n") devices published"
done
k get resourceslices
echo
echo "next: ./run-experiments.sh   (placement; tagging needs the real testbed)"
