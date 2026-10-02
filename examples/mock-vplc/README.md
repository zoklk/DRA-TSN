# mock-vplc — busy-poll round-trip probe (C)

Busy-poll C replacement for `scripts/probe-loop.py`. Runs **in the vPLC pod** and
models one PLC scan cycle driving one or more robot arms: each tick it sends a
command to every arm (TX fast path) and busy-polls for the actuator echoes — never
sleeping (ADR-0028).

It removes the two wakeup-latency sources the Python probe paid for:

| jitter source | probe-loop.py | mock_vplc |
|---------------|---------------|-----------|
| TX cadence | `time.sleep()` → timer wakeup | busy-wait on `CLOCK_TAI` |
| RX drain | blocking `recv()` thread → softirq/scheduler wakeup | non-blocking `recv(MSG_DONTWAIT)` spun on an isolated core + `SO_BUSY_POLL` |

Pair with **examples/mock-actuator** (XDP reflect) on the arm nodes: actuator-side
and pod-side wakeups are then both gone, leaving only the NIC↔pod data path.

## Multi-arm

A PLC commonly drives several arms in one cell. Each arm = a TX dst + an RX listen
port. The eBPF `tx_ts`/`rx_ts` timeline maps are `ARRAY[seq]`, so the arms' seq
ranges are **partitioned** — arm *i* uses `[i·span, i·span+COUNT)` with
`span = TS_MAX_SAMPLES/nArms` — to avoid clobbering each other's kernel timestamps.
`COUNT` must be ≤ `span` (e.g. 2 arms → span 32768, so COUNT ≤ 32768).

Output per arm: `send.<label>.csv` (seq,t0) + `recv.<label>.csv` (seq,t4). The first
arm also writes the canonical `send.csv`/`recv.csv` so `measure-stats.sh` works as-is
on arm-1. (arm-2 stats need a seq-base-aware `measure-collect` pass — follow-up.)

## Config (env)

Defaults = the cell-A two-arm topology; values mirror the NRI-injected `TSN_STREAMS`.

| env | default | meaning |
|-----|---------|---------|
| `ARMS` | `192.168.1.11,4840,4840,arm1 192.168.1.12,4840,4841,arm2` | space-separated `ip,txport,rxport,label` |
| `COUNT` | `10000` | packets per arm (≤ span) |
| `RATE` | `500` | send Hz (500 = 2 ms cycle, UR10e e-Series) |
| `BUSY_US` | `50` | `SO_BUSY_POLL` budget (µs) |
| `CPU` | unset | pin to this core (set to an isolated `VPLC_CPUS` core) |
| `GRACE` | `3` | seconds to keep draining late echoes |
| `MEASURE_DIR` | `/tmp` | CSV output dir |

## Build & run

Built static so it runs in any pod image (no toolchain in the container):

```bash
make                       # -> mock_vplc (static); or: make CC=musl-gcc
kubectl -n dratsn-test cp mock_vplc <pod>:/tmp/mock_vplc
kubectl -n dratsn-test exec -i <pod> -- env COUNT=10000 RATE=500 CPU=2 \
  MEASURE_DIR=/tmp /tmp/mock_vplc
```

`CPU` should be one of the pod's isolated cores (Guaranteed QoS pins it there — see
`docs/vplc-setup.md`). Then collect with `scripts/measure-collect.sh` +
`scripts/measure-stats.sh` exactly as for the Python probe.
