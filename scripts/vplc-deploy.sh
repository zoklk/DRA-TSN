#!/usr/bin/env bash
# Deploy/remove a vPLC pod with Guaranteed QoS so CPU Manager pins it to the
# isolated cores (docs/vplc-setup.md §4). requests==limits + integer cpu.
# Lands on isolated VPLC_CPUS only if the node is set up (static policy + isolcpus).
#
# Usage: ./vplc-deploy.sh apply     # create namespace + vPLC pod
#        ./vplc-deploy.sh delete     # remove it
#        ./vplc-deploy.sh status     # QoS class + pinned cpus
#        VPLC_CPU=2 VPLC_IMAGE=myrepo/vplc:1 ./vplc-deploy.sh apply
set -euo pipefail
. "$(dirname "$0")/env.sh"
have "$KUBECTL" || die "$KUBECTL not found"

manifest() {
cat <<YAML
apiVersion: v1
kind: Pod
metadata:
  name: ${VPLC_NAME}
  namespace: ${NS}
  labels: { app: ${VPLC_NAME} }
spec:
  # nodeSelector intentionally omitted — TSN-aware placement is the DRA driver's job.
  # For a bare isolation test, pin a node here or add a TSN ResourceClaim.
  containers:
  - name: ${VPLC_NAME}
    image: ${VPLC_IMAGE}
    resources:
      requests: { cpu: "${VPLC_CPU}", memory: "${VPLC_MEM}" }
      limits:   { cpu: "${VPLC_CPU}", memory: "${VPLC_MEM}" }   # ==requests + integer cpu => Guaranteed
YAML
}

case "${1:-status}" in
  apply)
    "$KUBECTL" get ns "$NS" >/dev/null 2>&1 || "$KUBECTL" create ns "$NS"
    manifest | "$KUBECTL" apply -f -
    ok "applied $NS/$VPLC_NAME (cpu=$VPLC_CPU Guaranteed)"
    info "verify QoS:  $0 status"
    ;;
  delete)
    manifest | "$KUBECTL" delete --ignore-not-found -f -
    ok "deleted $NS/$VPLC_NAME"
    ;;
  status)
    qos=$("$KUBECTL" -n "$NS" get pod "$VPLC_NAME" -o jsonpath='{.status.qosClass}' 2>/dev/null || true)
    [ -n "$qos" ] && ok "QoS = $qos (want Guaranteed)" || { warn "$NS/$VPLC_NAME not found"; exit 0; }
    node=$("$KUBECTL" -n "$NS" get pod "$VPLC_NAME" -o jsonpath='{.spec.nodeName}' 2>/dev/null || true)
    info "node = ${node:-?}"
    info "check pinned cpus ON THAT NODE:"
    printf '   cat /sys/fs/cgroup/kubepods.slice/.../%s*/cpuset.cpus.effective  # want %s\n' "$VPLC_NAME" "$VPLC_CPUS"
    ;;
  *) die "usage: $0 {apply|delete|status}" ;;
esac
