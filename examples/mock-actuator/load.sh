#!/usr/bin/env bash
# Attach/detach the actuator XDP reflect program on the TSN NIC.
# Run as root ON THE ACTUATOR NODE. `make` first to build reflect.bpf.o.
#
#   ./load.sh attach            # native XDP on $IFACE
#   MODE=generic ./load.sh attach   # fallback if the driver lacks native XDP_TX
#   ./load.sh detach
#   ./load.sh status
set -euo pipefail

IFACE="${IFACE:-eno1}"
OBJ="${OBJ:-$(dirname "$0")/reflect.bpf.o}"
SEC="${SEC:-xdp}"
MODE="${MODE:-native}"   # native | generic

flag() { [ "$MODE" = generic ] && echo xdpgeneric || echo xdp; }

case "${1:-status}" in
  attach)
    [ -f "$OBJ" ] || { echo "missing $OBJ — run 'make' first" >&2; exit 1; }
    # XDP sees the 802.1Q tag inline only if HW rx-VLAN stripping is OFF; with it
    # on, the tag is offloaded to metadata and reflect would miss every probe.
    ethtool -K "$IFACE" rxvlan off 2>/dev/null || true
    ip link set dev "$IFACE" "$(flag)" obj "$OBJ" sec "$SEC"
    echo "attached $OBJ ($SEC) on $IFACE [$MODE]"
    ;;
  detach)
    ip link set dev "$IFACE" "$(flag)" off
    echo "detached XDP on $IFACE"
    ;;
  status)
    ip -d link show dev "$IFACE" | grep -iE 'xdp|prog/id' || echo "no XDP on $IFACE"
    ;;
  *)
    echo "usage: $0 {attach|detach|status}   (env: IFACE MODE=native|generic)" >&2
    exit 1
    ;;
esac
