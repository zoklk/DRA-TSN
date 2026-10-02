#!/usr/bin/env bash
# Shared config for reproducible experiments (experiments/).
# Source from each run.sh: `. "$(dirname "$0")/../env.sh"`.
# Inherits scripts/env.sh (topology/measurement constants) and adds
# experiment-only knobs. Override anything via environment.

_EXP_ENV_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$_EXP_ENV_DIR/../scripts/env.sh"

# --- cluster access ---
: "${TSN_NODE:=alpha-w1}"          # node holding the TSN NIC
: "${TSN_NODE_IP:=192.168.110.12}" # mgmt IP of TSN_NODE (ssh)
: "${INJECT_NODE:=alpha-w2}"       # node used to inject RX frames on the isolated net
: "${INJECT_NODE_IP:=192.168.110.13}"
: "${SSH_USER:=netai}"
# SSH_PASS: set in your shell for password auth (never committed).
#   export SSH_PASS='...'   -> lib.sh node_ssh/node_scp use sshpass.
# Leave empty to use plain ssh (key-based auth).
: "${SSH_PASS:=}"

# --- experiment sizing ---
: "${TRIALS:=30}"      # control-plane repetitions per arm
: "${FRAMES:=200}"     # data-plane frames per sub-scenario

# --- python for plots ---
: "${EXP_VENV:=$_EXP_ENV_DIR/.venv}"
EXP_PY="$EXP_VENV/bin/python"

# --- streams (mirror of tests/loop.sh scenario; used to verify/restore mock-cnc) ---
# arm-1: 192.168.1.11 mac 0c:c4:7a:ca:3b:f2 (beta-w2)  s-tx (TX vlan200 pcp6) / s-rx (RX vlan100 pcp5, port 4840)
# arm-2: 192.168.1.12 mac 0c:c4:7a:ca:3c:02 (beta-w3)  s-tx2 / s-rx2 (RX port 4841)
: "${ARM1_IP:=192.168.1.11}"; : "${ARM1_MAC:=0c:c4:7a:ca:3b:f2}"
: "${ARM2_IP:=192.168.1.12}"; : "${ARM2_MAC:=0c:c4:7a:ca:3c:02}"
: "${TX_VLAN:=200}"; : "${TX_PCP:=6}"
# 802.1Qcc max-latency budget (ns) attached to every stream; per-node
# accumulated-latency is passed to ensure_streams (5th arg) to differentiate
# slack for the tsn-scheduler score. slack = MAX_LATENCY_NS - accumulated.
: "${MAX_LATENCY_NS:=1000000}"
