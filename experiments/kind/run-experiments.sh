#!/usr/bin/env bash
# Run the control-plane experiments against a kind cluster.
# fig_tagging is excluded: it needs the real NIC data plane.
#
# fig_mixed is NOT driven from here — it needs two clusters and its own staged
# loop (see ../fig_mixed/README.md).
set -eu
cd "$(dirname "$0")"
: "${KIND_CLUSTER:=dratsn}"
KNODE="$KIND_CLUSTER-worker1"     # K8s node name; the container is -worker

# Pin to a per-cluster kubeconfig instead of switching the shared
# current-context: `kind create cluster` rewrites that context as a side effect,
# so a concurrent create for another cluster would silently redirect every
# kubectl here (same reasoning as deploy.sh).
KUBECONFIG_FILE="$HOME/.kube/kind-$KIND_CLUSTER.kubeconfig"
kind export kubeconfig --name "$KIND_CLUSTER" --kubeconfig "$KUBECONFIG_FILE" >/dev/null
export KUBECONFIG="$KUBECONFIG_FILE"

export TSN_NODE="$KNODE" TSN_IFACE=eth0
# every worker, so the placement figure shows the whole pool a blind scheduler
# can reach, not just the TSN-capable corner of it
export EXP_NODES="$(kubectl get nodes -l '!node-role.kubernetes.io/control-plane' \
  -o jsonpath='{range .items[*]}{.metadata.name},{end}' | sed 's/,$//')"
# Assigned in two steps on purpose: `export VAR="$(...)"` makes the assignment
# the command whose status is checked, so a failing kubectl leaves VAR empty and
# set -e never fires. This used to look up a stale TSNNic name and silently
# produce an empty RX endpoint.
RX_ENDPOINT_IP="$(kubectl get tsnnic "$KNODE-eth0" -o jsonpath='{.spec.hostIP}')"
export RX_ENDPOINT_IP
[ -n "$RX_ENDPOINT_IP" ] || { echo "TSNNic $KNODE-eth0 has no hostIP — run ./deploy.sh first"; exit 1; }
: "${TRIALS:=10}"; export TRIALS
: "${RUN:=kind-$(date +%Y%m%d-%H%M%S)}"; export RUN

../fig_placement/run.sh

VENV=../.venv/bin/python
[ -x "$VENV" ] || VENV=python3
"$VENV" ../fig_placement/plot.py "../fig_placement/results/$RUN" || true
echo "results under fig_placement/results/$RUN"
