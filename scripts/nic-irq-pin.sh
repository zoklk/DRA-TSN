#!/usr/bin/env bash
# Steer TSN NIC IRQs to the dedicated isolated core (docs/vplc-setup.md §2 / actuator-setup.md §2).
# Run AFTER reboot (grub isolation params set by hand — vplc-setup.md §1). Idempotent —
# re-run after each reboot, or wrap in a systemd unit. Disables irqbalance first
# (else it redistributes affinity).
#
# Usage: sudo ./nic-irq-pin.sh            # pin $TSN_IFACE IRQs to core $NIC_CPU
#        sudo TSN_IFACE=eno6 NIC_CPU=4 ./nic-irq-pin.sh
set -euo pipefail
. "$(dirname "$0")/env.sh"
need_root

# stop irqbalance so static affinity sticks
if have systemctl; then
  systemctl stop irqbalance 2>/dev/null && info "stopped irqbalance" || true
  systemctl disable irqbalance 2>/dev/null || true
fi

irqs=$(grep -E "[[:space:]]${TSN_IFACE}" /proc/interrupts | awk '{print $1}' | tr -d ':')
[ -n "$irqs" ] || die "no IRQs found for $TSN_IFACE (check name / driver loaded)"

n=0
for irq in $irqs; do
  if echo "$NIC_CPU" > "/proc/irq/$irq/smp_affinity_list" 2>/dev/null; then
    n=$((n+1))
  else
    warn "IRQ $irq: cannot set affinity (managed IRQ? skipped)"
  fi
done
ok "pinned $n $TSN_IFACE IRQ(s) → core $NIC_CPU"

info "verify (eno IRQ rows should grow only on CPU$NIC_CPU):"
printf '   watch -n1 "grep %s /proc/interrupts"\n' "$TSN_IFACE"
for irq in $irqs; do
  printf '   IRQ %s -> CPU %s\n' "$irq" "$(cat /proc/irq/$irq/smp_affinity_list 2>/dev/null || echo '?')"
done
