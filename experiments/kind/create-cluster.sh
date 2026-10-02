#!/usr/bin/env bash
# Create the kind cluster for the control-plane reproduction path.
# Requires: docker, kind (https://kind.sigs.k8s.io — no root needed).
#
# KIND_CLUSTER selects the cluster name (default dratsn). A second cluster
# (e.g. KIND_CLUSTER=vanilla) gives the dual-cluster live demo its blind arm:
# kind names the nodes <cluster>-worker..<cluster>-worker6 automatically.
set -eu
cd "$(dirname "$0")"
: "${KIND_CLUSTER:=dratsn}"
command -v kind >/dev/null || { echo "kind not found — install: https://kind.sigs.k8s.io/docs/user/quick-start/#installation"; exit 1; }
kind get clusters 2>/dev/null | grep -qx "$KIND_CLUSTER" && { echo "cluster '$KIND_CLUSTER' already exists"; exit 0; }
CFG="$(mktemp)"; trap 'rm -f "$CFG"' EXIT
sed -e "s/^name: .*/name: $KIND_CLUSTER/" \
    -e "s/name: dratsn-worker/name: $KIND_CLUSTER-worker/" \
    kind-config.yaml > "$CFG"
kind create cluster --config "$CFG" --wait 120s
kubectl --context "kind-$KIND_CLUSTER" get nodes
