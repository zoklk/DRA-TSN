#!/usr/bin/env bash
# Deploy a SECOND DRA driver (fake GPU) onto the kind cluster, alongside
# tsn.dra.net (ADR-0039 §1).
#
# The GPU itself is not the point. The point is that two independent DRA drivers
# coexist and one Pod can hold a claim from each — that is the evidence for
# "ai-inference requires TSN + GPU" and for the standard-extension-point claim.
# fig_regime does not need this; only fig_mixed does, which is why it is a
# separate script from deploy.sh.
#
# Source is the read-only reference impl at <workspace>/dra-example-driver.
# It is deployed UNMODIFIED — everything specific to us lives in the values below.
#
# Prereqs verified once (2026-08-30) and recorded so they are not re-litigated:
#   - containerd CDI: NOT a prerequisite. kindest/node:v1.34.0 ships containerd
#     2.1.3, which sets enable_cdi = true by default. (Our own driver needs no CDI
#     at all — it injects env via NRI — but the reference driver does.)
#   - feature gates: ZERO needed. The reference repo's own demo kind config lists
#     nine, of which four do not exist in K8s 1.34; copying that block leaves a
#     cluster that never comes up.
#   - version skew: the reference driver is built against k8s.io/* v0.36.2 and runs
#     here on a 1.34.0 kubelet/apiserver. Verified working end to end (prepare ->
#     CDI spec -> containerd env injection) by the smoke test at the bottom.
set -eu
cd "$(dirname "$0")"
SRC="$(cd ../../../dra-example-driver && pwd)"
REG=dratsn.kind TAG=kind
IMAGE="$REG/dra-example-driver:$TAG"
: "${KIND_CLUSTER:=dratsn}"
: "${GPU_NODES:=$KIND_CLUSTER-worker2 $KIND_CLUSTER-worker3 $KIND_CLUSTER-worker4 $KIND_CLUSTER-worker5}"
: "${GPU_NUM_DEVICES:=2}"
: "${HELM_RELEASE:=dra-gpu}"

export KUBECTL="kubectl"
# Pin to the per-cluster kubeconfig — never the shared current-context, which
# `kind create cluster` rewrites as a side effect (same reason as deploy.sh).
KUBECONFIG_FILE="$HOME/.kube/kind-$KIND_CLUSTER.kubeconfig"
kind export kubeconfig --name "$KIND_CLUSTER" --kubeconfig "$KUBECONFIG_FILE" >/dev/null
export KUBECONFIG="$KUBECONFIG_FILE"

. ../lib.sh

# ADR-0039 §0 topology: worker2,3 = TSN+GPU (the contested pair), worker4,5 = GPU
# only, worker1 = TSN only, worker6 = general. A dedicated label — NOT
# tsn.dra.net/capable, which selects workers 1-3.
hdr "label GPU-capable nodes: $GPU_NODES"
for n in $GPU_NODES; do
  k label node "$n" gpu.example.com/present=true --overwrite >/dev/null
done

# The host Go toolchain is older than the reference repo's `go` directive, so the
# containerised build is the only path. Skip it when the image is already built —
# it is a from-scratch golang image pull otherwise.
if docker image inspect "$IMAGE" >/dev/null 2>&1; then
  info "image $IMAGE already present — skipping build"
else
  hdr "build $IMAGE (containerised: host Go is too old for the reference repo)"
  make -C "$SRC" -f deployments/container/Makefile ubuntu22.04 \
       REGISTRY="$REG" VERSION="$TAG"
fi

hdr "load $IMAGE into kind cluster $KIND_CLUSTER"
kind load docker-image --name "$KIND_CLUSTER" "$IMAGE" >/dev/null

VALUES="$(mktemp)"; trap 'rm -f "$VALUES"' EXIT
cat > "$VALUES" <<EOF
deviceProfile: "gpu"
image:
  repository: $REG/dra-example-driver
  tag: $TAG
  pullPolicy: IfNotPresent
kubeletPlugin:
  # Deliberately scarce. The chart default of 8 would make GPU a non-constraint
  # and nothing would ever contend for it; a small number makes the elastic
  # ai-training workload actually compete with guaranteed ai-inference.
  numDevices: $GPU_NUM_DEVICES
  nodeSelector:
    gpu.example.com/present: "true"
EOF

hdr "helm install $HELM_RELEASE (driver gpu.example.com, $GPU_NUM_DEVICES devices/node)"
helm upgrade --install "$HELM_RELEASE" "$SRC/deployments/helm/dra-example-driver" \
     --namespace kube-system -f "$VALUES" >/dev/null

# A kubelet that was already running when a NEW DRA driver appeared accepts pods
# with that driver's claims and then never requests a sandbox: no event, no
# error, just ContainerCreating forever. Restarting it after the plugin registers
# clears that state. (Same wedge fig_regime documents for the TSN driver, which
# its prepare_health heals by restarting kubelet; this makes it deterministic
# rather than something the first experiment run has to discover.)
hdr "restart kubelet on GPU nodes so they pick up the new driver"
for n in $GPU_NODES; do
  docker exec "$n" systemctl restart kubelet
done
for n in $GPU_NODES; do
  wait_for 120 sh -c "kubectl get node $n -o jsonpath='{.status.conditions[?(@.type==\"Ready\")].status}' | grep -qx True" \
    || die "$n did not return Ready after kubelet restart"
done

n_gpu=$(set -- $GPU_NODES; echo $#)
wait_for 120 sh -c "kubectl get resourceslices -o json | \
  python3 -c \"import json,sys; print(sum(1 for s in json.load(sys.stdin)['items'] if s['spec']['driver']=='gpu.example.com'))\" | \
  grep -qx $n_gpu" || die "gpu.example.com ResourceSlices did not reach $n_gpu"
pass "gpu.example.com: $n_gpu ResourceSlices published"

# Regression guard for the one collision the audit flagged: the chart renders a
# ValidatingAdmissionPolicy covering ALL resource.k8s.io resourceslices with
# validationActions [Deny]. It is supposed to short-circuit on a matchCondition
# that only fires for its own service account, leaving tsn.dra.net alone.
n_tsn=$(kubectl get resourceslices -o json | python3 -c \
  "import json,sys; print(sum(1 for s in json.load(sys.stdin)['items'] if s['spec']['driver']=='tsn.dra.net'))")
[ "$n_tsn" -gt 0 ] && pass "tsn.dra.net: $n_tsn ResourceSlices still published" \
                  || fail "tsn.dra.net ResourceSlices disappeared — check the GPU chart's ValidatingAdmissionPolicy"

kubectl get deviceclass
summary
echo
echo "smoke test (optional, proves prepare -> CDI -> env injection):"
echo "  kubectl apply -f $SRC/demo/examples/basic-resourceclaimtemplate/"
echo "  kubectl -n basic-resourceclaimtemplate logs pod0 | grep GPU_DEVICE"
