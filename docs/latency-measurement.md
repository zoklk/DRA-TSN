# 지연 측정 (round-trip 제어루프 지연)

> vPLC 명령 → actuator echo → vPLC 피드백 1사이클 지연·지터 실측.
> ★ **PTP 불요** — 전부 w1 한 클럭(t0/t4 pod, t1/t2/t3 eBPF), actuator는 echo만(시각 안 잼).
> 전제: [`vplc-setup.md`](vplc-setup.md)·[`actuator-setup.md`](actuator-setup.md). 근거: ADR-0024, ADR-0028.

---

## 0. 스테이지

```
t0 pod send → t1 host-veth(tsn_tx) → t2 NIC egress(tsn_tx_guard) ─wire→ actuator echo
                                                              ←wire─ t3 NIC ingress(tsn_rx) → t4 pod 수신
```

| 구간 | 의미 | 소스 |
|---|---|---|
| t0→t1 | app → host veth (TX) | pod t0, eBPF t1 |
| t1→t2 | eBPF 태깅+redirect (TX) | eBPF t1,t2 |
| **t2→t3** | **wire 왕복 + actuator echo** | eBPF t2,t3 |
| t3→t4 | eBPF DNAT+redirect → pod 소켓 (RX) | eBPF t3, pod t4 |
| **t0→t4** | **풀 round-trip = 1 제어사이클** | pod t0,t4 |

---

## 1. 계측 (구현됨)

- **상관 = payload seq**: probe가 UDP payload에 `[magic(4)][seq(4)][t0(8)]` 삽입 → 모든 지점 같은 seq로 join (`bpf/tsn_common.h`).
- **eBPF 스탬프** (`bpf/tx.c`·`rx.c`): t1/t2 = `tx_ts[seq]`, t3 = `rx_ts[seq]`, `bpf_ktime_get_tai_ns()`. seq-indexed ARRAY 맵(bpffs 핀). stale 슬롯은 stats가 `ts<run_start`로 필터(런 간 clear 불요).
- **엔드포인트**: t0/t4 = pod(`mock_vplc`, busy-poll, CLOCK_TAI). actuator = echo만(`examples/mock-actuator` XDP reflect).

---

## 2. 측정 절차

격리(선택, [`vplc-setup.md`]) + actuator reflect([`actuator-setup.md`]) 완료 후 — **한 방 커맨드**:

```bash
OUT=scripts/results/busy-poll/arm1 COUNT=10000 ./scripts/measure-roundtrip.sh
```
pod서 `mock_vplc` N발 → send/recv.csv + w1 eBPF tx_ts/rx_ts(`kubectl debug node`) → `stats.txt` 한 디렉토리에. (Claude: `/measure-roundtrip arm1 10000` — 실행+baseline 비교+README까지.)

> 수동: ① pod `mock_vplc`(또는 구 `probe-loop.py`) ② w1 `measure-collect.sh`(root) ③ `MEASURE_DIR=<dir> measure-stats.sh`.
> arm-2: `ARMS="192.168.1.12,4840,4841,arm2"`.

---

## 3. 지표 (보고)

- 각 구간 + CYCLE: **p50/p99/max** (mean 금지 — 결정성=최악, ADR-0024).
- **baseline vs 격리** 동일 N·rate 2회 → 구간별 델타. (busy-poll vs wakeup 예: `scripts/results/busy-poll/` vs `wakeup-latency/`.)

## 4. 범위 (정직)

- 측정 = **round-trip 사이클 지연** (switchless best-effort, 격리망). t2→t3는 왕복+echo(순수 one-way 아님).
- **이론 TSN bound(802.1Qbv) 아님** — switchless. "격리망 실측 결정성"으로 한정.
</content>
