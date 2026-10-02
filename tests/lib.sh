#!/usr/bin/env bash
# Common helpers for DRATSN smoke tests.
# Sourced by smoke_phase*.sh. Test-first: checks SKIP when a prerequisite
# (CRD, DaemonSet, binary, label) is absent, so running today produces a
# clear "not implemented yet" map rather than hard failures.

# --- config (override via env) ---
: "${NS:=dratsn-test}"                 # namespace for test workloads
: "${DRIVER:=tsn.dra.net}"             # DRA driver / API group
: "${APIGROUP:=tsn.dra.net}"           # CRD group
: "${DS_NS:=kube-system}"              # DaemonSet namespace
: "${DS_NAME:=dratsn}"                 # DRA driver DaemonSet name
: "${CTRL_NS:=kube-system}"            # controller namespace
: "${CTRL_NAME:=tsn-controller}"       # controller Deployment name
: "${MOCK_CNC_NS:=kube-system}"        # mock CNC namespace
: "${MOCK_CNC_NAME:=mock-cnc}"         # mock CNC Deployment/Service name
: "${TSN_NODE:=}"                      # node expected to host the stream (auto if empty)
: "${TSN_IFACE:=eno1}"                 # TSN-capable NIC on TSN_NODE
: "${RX_ENDPOINT_IP:=}"               # node TSN NIC host IP (RX receive point)
: "${RX_ENDPOINT_PORT:=4840}"          # OPC UA default
: "${KUBECTL:=kubectl}"

# --- counters ---
PASS=0; FAIL=0; SKIP=0
RED=$'\e[31m'; GRN=$'\e[32m'; YEL=$'\e[33m'; DIM=$'\e[2m'; RST=$'\e[0m'

pass() { PASS=$((PASS+1)); printf '%s[PASS]%s %s\n' "$GRN" "$RST" "$*"; }
fail() { FAIL=$((FAIL+1)); printf '%s[FAIL]%s %s\n' "$RED" "$RST" "$*"; }
skip() { SKIP=$((SKIP+1)); printf '%s[SKIP]%s %s\n' "$YEL" "$RST" "$*"; }
info() { printf '%s[ .. ]%s %s\n' "$DIM" "$RST" "$*"; }
hdr()  { printf '\n=== %s ===\n' "$*"; }

# require <cmd> : SKIP-friendly tool check, returns 1 if missing
require() { command -v "$1" >/dev/null 2>&1 || { skip "tool '$1' missing"; return 1; }; }

# k <args...> : kubectl wrapper
k() { "$KUBECTL" "$@"; }

# crd_exists <plural.group>
crd_exists() { k get crd "$1" >/dev/null 2>&1; }

# resource_exists <type> [args...]
resource_exists() { k get "$@" >/dev/null 2>&1; }

# wait_for <timeout_s> <cmd...> : poll until cmd succeeds, returns 0/1
wait_for() {
  local t="$1"; shift; local i=0
  while [ "$i" -lt "$t" ]; do "$@" >/dev/null 2>&1 && return 0; sleep 1; i=$((i+1)); done
  return 1
}

# summary : print totals, exit non-zero only on FAIL (SKIP is ok)
summary() {
  printf '\n--- %s: %s%d pass%s, %s%d fail%s, %s%d skip%s ---\n' \
    "${PHASE:-smoke}" "$GRN" "$PASS" "$RST" "$RED" "$FAIL" "$RST" "$YEL" "$SKIP" "$RST"
  [ "$FAIL" -eq 0 ]
}

ensure_ns() { k get ns "$NS" >/dev/null 2>&1 || k create ns "$NS" >/dev/null 2>&1 || true; }

# --- predicates usable directly AND with wait_for (run in current shell, so the
#     k() function is available — unlike `sh -c "k ..."` which loses it) ---

# deploy_available <name> <ns> : Deployment has >=1 available replica
deploy_available() {
  local n; n="$(k get deploy "$1" -n "$2" -o jsonpath='{.status.availableReplicas}' 2>/dev/null)"
  [ -n "$n" ] && [ "$n" -ge 1 ] 2>/dev/null
}

# ds_ready <name> <ns> : DaemonSet has >=1 ready pod
ds_ready() {
  local n; n="$(k get ds "$1" -n "$2" -o jsonpath='{.status.numberReady}' 2>/dev/null)"
  [ -n "$n" ] && [ "$n" -ge 1 ] 2>/dev/null
}

# has_rows <type> [extra args...] : `kubectl get` returns at least one object
has_rows() { [ -n "$(k get "$@" -o name 2>/dev/null)" ]; }

# jget <jsonpath> <type/name> [args...] : print a jsonpath value
jget() { local jp="$1"; shift; k get "$@" -o jsonpath="$jp" 2>/dev/null; }
