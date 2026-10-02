#!/usr/bin/env bash
# Shared config for DRA-TSN experiment-environment setup scripts (scripts/).
# Source this from each script: `. "$(dirname "$0")/env.sh"`.
# Override any value via environment, e.g. `sudo NIC_CPU=8 ./nic-irq-pin.sh`.
#
# Core layout (12-core example, see docs/vplc-setup.md §0):
#   SYSTEM_CPUS  0,1   system/housekeeping, general IRQs, cluster NIC, kubelet, DRA-TSN driver (NOT isolated)
#   VPLC_CPUS    2,3   vPLC scan-cycle compute (isolated + Guaranteed QoS)
#   NIC_CPU      4     TSN NIC packet processing — eno1 IRQ + RX eBPF (isolated)
#   (5..11 free for general pods)

# --- core layout ---
: "${SYSTEM_CPUS:=0,1}"       # reserved for OS/kube; general IRQs land here
: "${VPLC_CPUS:=2,3}"         # vPLC compute, isolated + Guaranteed QoS
: "${NIC_CPU:=4}"             # TSN NIC processing core, isolated
# grub isolation params (isolcpus/nohz_full/rcu_nocbs/cstate) are edited by hand —
# see docs/vplc-setup.md §1 / actuator-setup.md §1. NIC_CPU below is still used by nic-irq-pin.sh.

# --- interfaces ---
: "${TSN_IFACE:=eno1}"        # TSN-capable NIC (matches tests/lib.sh)

# --- k8s ---
: "${NS:=dratsn-test}"        # vPLC workload namespace (matches tests/lib.sh)
: "${KUBECTL:=kubectl}"
: "${VPLC_NAME:=vplc}"        # vPLC pod/deploy name
: "${VPLC_IMAGE:=registry.k8s.io/pause:3.10}"  # placeholder; replace with real vPLC image
: "${VPLC_CPU:=2}"            # integer cpu request==limit (Guaranteed). must match VPLC_CPUS count
: "${VPLC_MEM:=1Gi}"

# --- round-trip latency measurement (ADR-0028, docs/latency-measurement.md) ---
# Cycle = pod(TX)-> actuator -> echo -> pod(RX). ALL timestamps on the w1 clock
# (t0/t4 in pod, t1/t2/t3 in eBPF) -> NO PTP / cross-node clock sync needed.
: "${PIN_DIR:=/sys/fs/bpf/tsn}"  # bpffs pin dir (matches ebpf.DefaultPinDir); tx_ts/rx_ts live here
: "${TS_MAGIC:=0x54534E31}"      # payload probe marker "TSN1" (matches tsn_common.h TS_MAGIC)
: "${TS_MAX_SAMPLES:=65536}"     # seq wraps mod this (matches TS_MAX_SAMPLES)
# Two robot arms in cell-A (same VLAN/PCP, distinct IP+MAC; RX node-endpoint ports
# split so each arm DNATs to its own pod socket). Defaults = arm-1 (primary).
#   arm-1: .11 mac 0c:c4:7a:c4:a7:2a  streams s-tx/s-rx   RX port 4840
#   arm-2: .12 mac 0c:c4:7a:ca:3c:3a  streams s-tx2/s-rx2 RX port 4841
: "${TX_DST_IP:=192.168.1.11}"   # pod->actuator target (arm-1 TX stream listener)
: "${TX_DST_PORT:=4840}"         # OPC UA / probe UDP port
: "${RX_ENDPOINT_IP:=192.168.1.1}"  # actuator echoes here (w1 RX endpoint -> DNAT to pod)
: "${RX_ENDPOINT_PORT:=4840}"    # arm-1 RX node-endpoint port (arm-2 = 4841)
: "${RX_VLAN:=100}"; : "${RX_TALKER_IP:=192.168.1.11}"  # echo frame vlan/src (rx_flows key; arm-1)
: "${PROBE_COUNT:=10000}"        # packets per run (must be < TS_MAX_SAMPLES)
: "${PROBE_RATE_HZ:=500}"        # send rate (500 = 2ms control cycle, UR10e e-Series)
: "${MEASURE_DIR:=/tmp/dratsn-measure}"  # CSV output dir

# --- w1 ssh (interference experiment: map dump + PSI without kubectl-debug) ---
# Password auth only (no keys, by operator decision). Export W1_PASS to enable;
# scripts that need it die with a hint when unset. sudo on w1 is passwordless for
# bpftool only (/etc/sudoers.d/dratsn-bpftool).
: "${W1_HOST:=192.168.110.12}"
: "${W1_USER:=netai}"
: "${W1_PASS:=}"
w1ssh() {  # w1ssh <cmd...> — run a command on w1 over password ssh
  [ -n "$W1_PASS" ] || die "W1_PASS not set (export W1_PASS=<w1 password>)"
  sshpass -p "$W1_PASS" ssh -o StrictHostKeyChecking=no -o PubkeyAuthentication=no \
    "$W1_USER@$W1_HOST" "$@"
}

# --- ui helpers ---
RED=$'\e[31m'; GRN=$'\e[32m'; YEL=$'\e[33m'; DIM=$'\e[2m'; RST=$'\e[0m'
info() { printf '%s[ .. ]%s %s\n' "$DIM" "$RST" "$*"; }
ok()   { printf '%s[ ok ]%s %s\n' "$GRN" "$RST" "$*"; }
warn() { printf '%s[warn]%s %s\n' "$YEL" "$RST" "$*"; }
die()  { printf '%s[fail]%s %s\n' "$RED" "$RST" "$*" >&2; exit 1; }

need_root() { [ "$(id -u)" -eq 0 ] || die "run as root (sudo)"; }
have()      { command -v "$1" >/dev/null 2>&1; }
confirm() {  # confirm "msg" ; honor FORCE=1
  [ "${FORCE:-0}" = 1 ] && return 0
  printf '%s [y/N] ' "$1"; read -r a; [ "$a" = y ] || [ "$a" = Y ]
}
