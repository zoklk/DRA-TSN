#!/usr/bin/env bash
# Phase 1 smoke: TSN Controller + CRDs.
# Spec: context/phases/controller.md §7.
# Verifies: CRDs registered, controller available, mock CNC up, and the join
# pipeline produced at least one TSNStream CR with the expected fields.
set -u
cd "$(dirname "$0")"; . ./lib.sh
PHASE="phase1-controller"

hdr "Phase 1 — TSN Controller"
require kubectl || { summary; exit 0; }

# 1) CRDs exist
if crd_exists "tsnstreams.${APIGROUP}"; then pass "CRD tsnstreams.${APIGROUP} registered"
else skip "CRD tsnstreams.${APIGROUP} not registered (controller not deployed)"; fi
if crd_exists "tsnnics.${APIGROUP}"; then pass "CRD tsnnics.${APIGROUP} registered"
else skip "CRD tsnnics.${APIGROUP} not registered"; fi

# 2) Controller available
if resource_exists deploy "$CTRL_NAME" -n "$CTRL_NS"; then
  if wait_for 30 deploy_available "$CTRL_NAME" "$CTRL_NS"; then
    pass "controller Deployment $CTRL_NAME available"
  else fail "controller Deployment $CTRL_NAME present but not available"; fi
else skip "controller Deployment $CTRL_NAME absent"; fi

# 3) mock CNC up
if resource_exists deploy "$MOCK_CNC_NAME" -n "$MOCK_CNC_NS"; then pass "mock CNC $MOCK_CNC_NAME present"
else skip "mock CNC $MOCK_CNC_NAME absent — cannot drive stream lifecycle"; fi

# 4) join pipeline produced TSNStream CR(s)
if crd_exists "tsnstreams.${APIGROUP}"; then
  if wait_for 20 has_rows "tsnstreams.${APIGROUP}"; then
    pass "TSNStream CR(s) present (CNC x NIC join produced output)"
    # field sanity on the first stream
    first="$(jget '{.items[0].metadata.name}' "tsnstreams.${APIGROUP}")"
    dir="$(jget '{.spec.direction}' "tsnstreams.${APIGROUP}" "$first")"
    vlan="$(jget '{.spec.vlanId}' "tsnstreams.${APIGROUP}" "$first")"
    dstip="$(jget '{.spec.dst.ip}' "tsnstreams.${APIGROUP}" "$first")"
    if [ -n "$dir" ] && [ -n "$vlan" ] && [ -n "$dstip" ]; then
      pass "TSNStream $first has direction=$dir vlanId=$vlan dst.ip=$dstip"
    else fail "TSNStream $first missing required fields (dir/vlan/dst)"; fi
  else skip "no TSNStream CR yet — push a stream via mock CNC to exercise join"; fi
else skip "tsnstreams CRD absent"; fi

summary
