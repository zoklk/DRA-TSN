#!/usr/bin/env bash
# Phase 2 smoke: DRA Plugin (NIC discovery, ResourceSlice, TSN-aware placement).
# Spec: context/phases/dra.md §7.
set -u
cd "$(dirname "$0")"; . ./lib.sh
PHASE="phase2-dra"

hdr "Phase 2 — DRA Plugin"
require kubectl || { summary; exit 0; }

# 1) DaemonSet ready
if resource_exists ds "$DS_NAME" -n "$DS_NS"; then
  if wait_for 30 ds_ready "$DS_NAME" "$DS_NS"; then
    pass "DRA DaemonSet $DS_NAME ready"
  else fail "DaemonSet $DS_NAME present but no ready pods"; fi
else skip "DRA DaemonSet $DS_NAME absent"; fi

# 2) TSNNic published by discovery
if crd_exists "tsnnics.${APIGROUP}"; then
  if wait_for 20 has_rows "tsnnics.${APIGROUP}"; then
    pass "TSNNic CR(s) published by NIC discovery"
  else skip "no TSNNic CR — label a node's TSN NIC (tsn.dra.net/capable-nics)"; fi
else skip "tsnnics CRD absent"; fi

# 3) DeviceClass applied
if resource_exists deviceclass "$DRIVER"; then pass "DeviceClass $DRIVER present"
else skip "DeviceClass $DRIVER absent"; fi

# 4) ResourceSlice advertises TSN devices
if k get resourceslices >/dev/null 2>&1; then
  if k get resourceslices -o json 2>/dev/null | jq -e --arg d "$DRIVER" '.items[]?|select(.spec.driver==$d)|.spec.devices[]?' >/dev/null 2>&1; then
    pass "ResourceSlice with driver $DRIVER advertises device(s)"
  else skip "no ResourceSlice device for $DRIVER — needs published streams"; fi
else skip "resourceslices API unavailable"; fi

# 5) TSN-aware placement: vplc pod scheduled on the stream's node, claim allocated
POD="$(k get pods -n "$NS" -l app=vplc -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)"
if [ -n "$POD" ]; then
  phase="$(jget '{.status.phase}' pod "$POD" -n "$NS")"
  node="$(jget '{.spec.nodeName}' pod "$POD" -n "$NS")"
  # the node that owns a TSN ResourceSlice (pool == node name)
  tsn_node="$(k get resourceslices -o json 2>/dev/null | jq -r --arg d "$DRIVER" '.items[]|select(.spec.driver==$d)|.spec.pool.name' 2>/dev/null | head -1)"
  if [ -n "$node" ] && [ "$node" = "$tsn_node" ]; then
    pass "vplc pod $POD placed on TSN node $node (matches ResourceSlice owner) [phase=$phase]"
  else fail "vplc pod $POD on node '$node' != TSN node '$tsn_node' (placement incorrect)"; fi
  # claim allocated to a tsn device
  alloc="$(k get resourceclaim -n "$NS" -o json 2>/dev/null | jq -r '[.items[]|select(.status.allocation!=null)]|length' 2>/dev/null)"
  if [ "${alloc:-0}" -ge 1 ]; then pass "ResourceClaim allocated ($alloc) to a TSN device"
  else fail "no ResourceClaim allocated in ns $NS"; fi
else
  skip "no vplc pod in ns $NS — apply deployments/examples/vplc-tx.yaml to test placement"
fi

summary
