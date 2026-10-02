# scripts/ — 실험 환경 세팅 (experiment-env setup)

호스트/클러스터를 **측정 가능한 조건으로 조성**하는 스크립트. `tests/`(소프트웨어 정확성
검증)와 목적이 다르다 — 여기는 "코드가 맞나"가 아니라 "결정성 측정을 위한 환경을 만든다".

| 축 | 디렉토리 | 무엇 |
|---|---|---|
| 소프트웨어 dev-test 루프 | `tests/` (`loop.sh`, `smoke_*`) | DRA-TSN 빌드→배포→정확성 검증 |
| **실험 환경 조성** | **`scripts/`** | CPU 격리 / round-trip 측정 / 측정용 vPLC |

근거 문서(왜): `docs/vplc-setup.md`, `docs/actuator-setup.md`, `docs/latency-measurement.md`,
ADR-0028. 여기 스크립트는 그 절차의 **자동화**다. (지연 측정 = round-trip, **PTP 불요**.)

## 스크립트

| 스크립트 | 목적 | 실행 |
|---|---|---|
| `env.sh` | 공유 설정(코어 배치/iface/측정). 각 스크립트가 source. 환경변수로 override | (직접 실행 안 함) |
| `nic-irq-pin.sh` | TSN NIC IRQ를 전용 격리 코어로 steer (재부팅 후 매번) | `sudo TSN_IFACE=eno1 NIC_CPU=4 ./nic-irq-pin.sh` |
| `vplc-deploy.sh` | 측정용 vPLC pod 배포 (Guaranteed QoS = 격리 코어 점유) | `./vplc-deploy.sh apply\|delete\|status` |
| `probe-loop.py` | **pod**: round-trip 송신(t0)+수신(t4) 동시 → send.csv/recv.csv (C busy-poll 대안: `examples/mock-vplc` — wakeup 지터 제거) | `kubectl exec -i mock-vplc -- python3 - < probe-loop.py` |
| `echo-ext.py` | **외부노드**: raw 캡처 → w1 RX 엔드포인트로 echo 반사 (시각 안 잼) | `sudo NODE_MAC=<w1 mac> python3 echo-ext.py` |
| `measure-roundtrip.sh` | **한 방 측정** — pod서 mock_vplc N발 → send/recv.csv + w1 eBPF 맵(debug node) → 4 CSV + stats 한 디렉토리에 | `OUT=results/x COUNT=10000 ./measure-roundtrip.sh` (m1서) |
| `measure-collect.sh` | eBPF `tx_ts`/`rx_ts` 맵 `bpftool` 덤프 → CSV (위 래퍼가 내부적으로 하는 일을 w1서 직접 할 때) | `sudo ./measure-collect.sh` (vPLC 노드) |
| `measure-stats.sh` | seq join → 구간별 p50/p99/max + CYCLE (stale 필터, mean 금지) | `MEASURE_DIR=… ./measure-stats.sh` |
| `verify-dataplane.sh` | RX 데이터플레인 검증 — w2서 vlan100 주입 → pod 도달(strip+DNAT+redirect) 확인 → `results/rx_*.log` | `./verify-dataplane.sh` |

### 간섭 측정 — 전제검증 전용 (ADR-0035)

메모리 대역폭 간섭 Score는 **기여로서 폐기**됐다(ADR-0035, 2026-08-11). 아래 세
스크립트는 "격리 스택으로 충분하므로 실패 축은 간섭이 아니라 N" 이라는 **전제검증
절의 근거로만** 존치한다. 시뮬레이션 보정 소스가 아니다(ADR-0036: 지연 모델은
선언 분포 + 문헌 앵커 + `sweep.py` 민감도).

| 스크립트 | 목적 |
|---|---|
| `run-interference.sh` | 간섭 유무 대조 측정 드라이버 — `results/interference/<RUN>/` |
| `interference-load.sh` | 간섭 부하 생성 (메모리 대역폭 점유) |
| `measure-hostpath.sh` | host egress t0→t2 구간 측정 |
| `probe-loop-rt.py` | `probe-loop.py`의 실시간 변종 |
| `echo-udp.py` | 단순 UDP 에코 (`echo-ext.py`의 raw 캡처 없는 판) |

인용되는 수집분: `results/interference/v2-smt-split`
(ADR-0036, `context/dratsn-interference-scenario-design.md` §4-1).

> **TX 검증은 w1 호스트서 직접** (`sudo tcpdump`) — m1→worker NIC는 `kubectl debug node`라 긴 tcpdump 불안정. run_book §1 참조:
> `sudo tcpdump -i eno1 -e -nn 'vlan 200 and dst host 192.168.1.11'` (eno1=태깅 後) + `sudo tcpdump -i <lxc...> -e -nn 'udp and dst host 192.168.1.11'` (veth=원본). 트리거: `kubectl -n dratsn-test exec mock-vplc -- sh -c 'for i in $(seq 20); do echo -n X|nc -u -w1 192.168.1.11 4840; done'` (arm-1; arm-2=192.168.1.12)

## 표준 셋업 순서 (워커 노드)

> 코어 격리(grub isolcpus/nohz/rcu/cstate)는 **손으로 편집** — 절차/토큰은 `docs/vplc-setup.md §1`.
> 여기 스크립트는 reboot 後 단계만 자동화.

```bash
# 1. 코어 격리 = docs/vplc-setup.md §1 (grub 수동 편집) → sudo reboot

# 2. 재부팅 후 — TSN NIC IRQ를 전용 코어로 (★ 변수는 sudo 뒤)
sudo TSN_IFACE=eno1 NIC_CPU=4 ./nic-irq-pin.sh

# 3. 측정용 vPLC 배포 (Guaranteed QoS, netshoot 이미지)
./vplc-deploy.sh apply
./vplc-deploy.sh status        # QoS=Guaranteed, 격리 코어 점유 확인

# 되돌리기: vPLC 삭제 + irqbalance 복원 + grub 토큰 수동 제거 후 reboot
./vplc-deploy.sh delete
sudo systemctl enable --now irqbalance
```

## round-trip 지연 측정 (PTP 불요 — 전부 w1 클럭)

```bash
# 1. 로봇팔 노드(actuator)서 echo 기동 (arm-1 .11). userspace echo-ext.py 또는
#    XDP reflect(권장, 지터↓): examples/mock-actuator 참조 — `make && sudo ./load.sh attach`
sudo IFACE=eno1 NODE_MAC=<w1-eno1-mac> COUNT=10000 python3 echo-ext.py

# 2. pod서 송수신 루프 (m1서 주입)
kubectl -n dratsn-test exec -i mock-vplc -- env DST_IP=192.168.1.11 RX_PORT=4840 \
  COUNT=10000 RATE=500 MEASURE_DIR=/tmp python3 - < probe-loop.py

# 3. 수집 — w1서 tx_ts/rx_ts 덤프, pod서 send/recv.csv 회수 → MEASURE_DIR
# 4. 통계
MEASURE_DIR=<dir> ./measure-stats.sh   # 구간별 t0t1/t1t2/t2t3/t3t4 + CYCLE(t0t4)
```
구간: app→veth, veth→NICeg, **net+echo(왕복)**, NICing→pod, **CYCLE(제어루프 1사이클)**. baseline vs 격리 비교.
- `ARM=fast`(기본, eBPF 스탬프) vs `ARM=best-effort`(NodePort, endpoint만) 2-arm 대조.
- CSV·통계 = `$MEASURE_DIR`(기본 `/tmp/dratsn-measure`). 구간별 p50/p99/max.
- ⚠️ 측정용 vPLC는 **python3 포함 이미지** 필요(busybox 불가) — `VPLC_IMAGE` 교체.

## 코어 배치 (기본값, `env.sh`로 변경)

```
SYSTEM_CPUS=0,1   시스템/IRQ/kubelet/DRA-TSN driver (격리 안 함)
VPLC_CPUS=2,3     vPLC 연산 (격리 + Guaranteed QoS)
NIC_CPU=4         TSN NIC 처리 = eno1 IRQ + RX eBPF (격리)
5~11              자유
```
변경 예: `sudo NIC_CPU=8 ./nic-irq-pin.sh` (grub 코어 배치는 vplc-setup.md §1서 수동 지정).

## ⚠️ 측정용 vPLC ≠ 테스트 fixture vPLC

- **`scripts/vplc-deploy.sh`** = 측정용. **Guaranteed QoS**, 격리 코어 점유. 지연/지터 측정 조건.
- **`tests/loop.sh scenario`**(+`deployments/examples/vplc-*.yaml`) = 기능 fixture. 배치/태깅/DNAT
  *정확성* 검증용. QoS 무관.

목적이 달라 의도적으로 분리 — 합치지 말 것.
