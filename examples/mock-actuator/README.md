# mock-actuator — XDP reflect (round-trip latency harness)

Kernel/XDP replacement for `scripts/echo-ext.py`. Reflects each vPLC TX probe frame
straight back from the actuator NIC's **XDP ingress** hook — stateless, no userspace
loop — so the actuator side no longer adds wakeup + Python (GIL) jitter to the
measured round trip (ADR-0028).

This is **measurement scaffolding**, not DRATSN data plane: it runs on a *robot-arm*
node (an external "sensor/actuator"), uses no maps, and is loaded by hand with
`ip link set <iface> xdp`. That is why it lives under `examples/` instead of `bpf/`
(which is the Go-loaded data plane).

cell-A has **two robot arms** (same VLAN/PCP, distinct IP+MAC, split RX ports). Build
one object per arm — defaults are arm-1; pass `-D` for arm-2 (see Config):

| arm | node IP | talker MAC | streams | RX node-endpoint port |
|-----|---------|------------|---------|-----------------------|
| arm-1 | `192.168.1.11` | `0c:c4:7a:c4:a7:2a` | s-tx / s-rx | 4840 |
| arm-2 | `192.168.1.12` | `0c:c4:7a:ca:3c:3a` | s-tx2 / s-rx2 | 4841 |

## What it does

A vPLC TX probe arrives already 802.1Q-tagged (the TX data plane tagged it):

```
eth[src=w1 NIC, dst=this NIC] · 802.1Q[TX vid/pcp] · IP[src=pod, dst=this node] · UDP[dst=4840] · payload[magic|seq|t0]
```

`tsn_reflect` rewrites it for the **w1 RX TSN endpoint** so the w1 RX data plane
(`tsn_rx`) DNATs it into the pod, then `XDP_TX` bounces it out the same NIC:

| field | action |
|-------|--------|
| L2 MAC | **swap** (dst ← w1 NIC, src ← this NIC) |
| 802.1Q VID/PCP | **rewrite** to the RX stream tag (differs from the TX tag) |
| IP src/dst | **set** talker → node-endpoint (static, *not* a swap — `rx_flows` keys on `{talker_ip, node_endpoint_ip}`) |
| UDP dst port | **set** to `ECHO_DPORT` (this arm's RX node-endpoint port; arm-2 = 4841) |
| UDP checksum | zeroed (legal for IPv4); src port untouched |
| payload `magic|seq|t0` | **preserved** (pod pairs the echo to its send) |

Non-probe frames → `XDP_PASS` (normal stack), so the actuator stays reachable.

## Config

Defaults = arm-1, mirroring `scripts/env.sh`. Override at build with `CFLAGS_EXTRA`
(see `reflect.h`):

| macro | default (arm-1) | env.sh source |
|-------|-----------------|---------------|
| `ECHO_SRC_IP` | `192.168.1.11` (`0xC0A8010B`) | `RX_TALKER_IP` |
| `ECHO_DST_IP` | `192.168.1.1`  (`0xC0A80101`) | `RX_ENDPOINT_IP` |
| `ECHO_DPORT`  | `4840` | RX node-endpoint port |
| `ECHO_VID`    | `100`  | `RX_VLAN` |
| `ECHO_PCP`    | `5`    | RX stream priority |

```bash
# arm-2 (192.168.1.12, RX port 4841):
make CFLAGS_EXTRA='-DECHO_SRC_IP=0xC0A8010Cu -DECHO_DPORT=4841'
```

## Build & run (on each robot-arm node, as root)

Build the matching object on each arm (arm-1 = defaults, arm-2 = the `-D` overrides
above), then attach:

```bash
make                          # arm-1: -> reflect.bpf.o (needs clang + libbpf-dev)
sudo ./load.sh attach         # native XDP on $IFACE (default eno1)
sudo MODE=generic ./load.sh attach   # fallback if the driver lacks native XDP_TX
sudo ./load.sh status
sudo ./load.sh detach
```

`load.sh attach` triggers the verifier — a clean attach means the program loaded.
The I350 (igb) / X553 (ixgbe) NICs support native `XDP_TX` on kernel 6.8; use
`MODE=generic` only if native attach is rejected.

`load.sh attach` also runs `ethtool -K $IFACE rxvlan off`: XDP only sees the 802.1Q
tag inline when HW rx-VLAN stripping is disabled — otherwise the tag is offloaded to
metadata and the reflect (which parses the tag) misses every probe.

Replaces the old actuator step:
`sudo IFACE=eno1 NODE_MAC=<w1 eno1 MAC> python3 echo-ext.py` → just `./load.sh attach`
(MAC is handled by the L2 swap, no `NODE_MAC` needed).

## Isolation (robot-arm node) & revert

XDP_TX runs in the NIC's NAPI/softirq context — there is **no userspace echo process**,
so unlike the vPLC node this needs only **NIC-core isolation**, not compute-core
isolation. Full setup + revert: [`docs/actuator-setup.md`](../../docs/actuator-setup.md).
The essentials:

- **NIC core**: pin the `eno1` IRQ to one core + `isolcpus` it (no `nohz_full`/taskset/
  Guaranteed needed — no process to pin).
- **rxvlan off**: handled by `load.sh attach`.
- **C-state cap** (important): with no busy-poll, an idle NIC core sleeps between the
  1 ms packets and the C-state exit latency on the next IRQ shows up as jitter. Cap it:
  `sudo cpupower idle-set -D 1` (or grub `intel_idle.max_cstate=1 processor.max_cstate=1`).

Revert to the original state:
```bash
sudo ./load.sh detach
sudo ethtool -K eno1 rxvlan on        # undo the rxvlan off from attach
sudo cpupower idle-set -E             # re-enable C-states
sudo systemctl enable --now irqbalance  # undo the IRQ pin
# only if you put isolcpus/max_cstate in grub: edit GRUB_CMDLINE_LINUX to drop those
# tokens, then:  sudo update-grub && sudo reboot   (see docs/actuator-setup.md §6)
```

## Validate the effect

Re-run the round-trip measurement (vPLC pod side unchanged) and compare the
`net+echo` (t2→t3) stage against `scripts/results/roundtrip-iso`. The actuator's
userspace echo was the dominant jitter source; with XDP reflect the t2→t3 stdev
should drop sharply — confirming the wakeup-latency hypothesis.
