# vPLC 노드 셋업 런북

> vPLC(제어연산) Pod가 도는 노드를 세팅하는 방법. 임계경로 코어(vPLC 연산 + NIC 패킷처리)를
> 격리해 제어루프 지연을 결정적으로 만든다. provisioning(TSN 스케줄링)과 무관 — 런타임
> 데이터플레인 전용. 지연 측정([`latency-measurement.md`](latency-measurement.md))의 전제.

---

## 0. 노드 식별 + 전제 + 코어 배치

12코어 노드 기준 배치:

| 코어 | 역할 | 격리 |
|---|---|---|
| 0,1 | 시스템/housekeeping · 일반 IRQ · kubelet · DRA-TSN driver | ✗ |
| **2,3** | vPLC 연산 (scan cycle) | ✓ + Guaranteed QoS |
| **4** | TSN NIC(eno1) 패킷처리 — eno1 IRQ + RX eBPF | ✓ |
| 5~11 | 일반 Pod | ✗ |

> TSN NIC을 0,1에 두지 말 것 — 0,1은 OS 소음·일반 IRQ 몰리는 코어. NIC 처리(RX eBPF)는 격리코어(4) 필요.

```bash
ethtool -i eno1 | grep driver      # 커널 드라이버 바인딩 (vfio-pci면 → actuator-setup.md 부록 A)
nproc                              # 코어수 (배치 조정 기준)
```
> TSN NIC = `eno1`, 연산코어 = `2,3`, NIC코어 = `4` 가정. 다르면 이하 치환.

---

## 1. grub — 코어 격리 + C-state 캡 (reboot 1회)

`/etc/default/grub`의 `GRUB_CMDLINE_LINUX`에 추가 (기존 isolcpus/nohz/rcu/irqaffinity 토큰 있으면 교체):
```
isolcpus=2,3,4 nohz_full=2,3 rcu_nocbs=2,3,4 irqaffinity=0,1 intel_idle.max_cstate=1 processor.max_cstate=1
```
- `isolcpus=2,3,4` — 스케줄러가 일반 태스크 안 올림 (연산 2,3 + NIC 4).
- `nohz_full=2,3` — 연산코어 tickless(주기 타이머 tick = jitter 주범 제거). NIC코어(4)는 softirq 코어라 제외.
- `rcu_nocbs=2,3,4` — RCU 콜백 다른 코어로 offload.
- `irqaffinity=0,1` — 일반 IRQ를 0,1로 (격리코어 회피). eno1만 §2에서 코어4 예외.
- `*.max_cstate=1` — 깊은 C-state exit latency jitter 제거.

```bash
sudo update-grub && sudo reboot
```
---

## 2. reboot 후 — NIC IRQ 핀 (★ 매 reboot 필요)

```bash
cd ~/dratsn
sudo TSN_IFACE=eno1 NIC_CPU=4 ./scripts/nic-irq-pin.sh
```

→ 연산코어(2,3) = IRQ 0개, NIC코어(4) = eno1 IRQ만.

---

## 3. cilium XDP 가속 끄기 (클러스터 1회, 영구)

RX 데이터플레인이 우리 XDP로 NIC hook을 소유하려면 cilium XDP 가속 **Disabled** 전제:
```bash
kubectl -n kube-system edit cm cilium-config     # bpf-lb-acceleration: disabled
kubectl -n kube-system rollout restart ds/cilium
```

---

## 4. mock-vPLC 배포 + 측정 바이너리 실행

측정 코드 = `examples/mock-vplc`의 **static C 바이너리**(`mock_vplc`, busy-poll). 이미지 빌드 안 함 —
netshoot pod에 바이너리만 `cp`해서 실행.

```bash
# 1. pod 띄우기 (netshoot + TSN ResourceClaim)
kubectl apply -f deployments/examples/mock-vplc.yaml      # 또는 ./scripts/vplc-deploy.sh apply
# 2. 바이너리 빌드 + 주입
cd examples/mock-vplc && make                             # → mock_vplc (static)
kubectl -n dratsn-test cp mock_vplc mock-vplc:/tmp/mock_vplc
# 3. 격리코어(2)에 핀해서 실행
kubectl -n dratsn-test exec -i mock-vplc -- \
  env COUNT=10000 RATE=500 CPU=2 MEASURE_DIR=/tmp /tmp/mock_vplc
```
`CPU=2` = 바이너리가 `sched_setaffinity`로 코어2에 **자기핀**. isolcpus가 2,3,4를 비워둬 그 코어는
정숙 → **kubelet CPU Manager·Guaranteed QoS 불필요.** (mock-vplc.yaml의 `resources: Guaranteed`는
self-pin엔 무관 — 있어도/없어도 측정 동일.)


---

## 5. 검증 (전부 통과해야 완료)

```bash
cat /sys/devices/system/cpu/isolated                       # → 2-4
cat /sys/devices/system/cpu/nohz_full                      # → 2-3
grep -o 'max_cstate=[0-9]' /proc/cmdline                   # intel_idle/processor 둘 다 =1
for i in $(grep eno1 /proc/interrupts|awk '{print $1}'|tr -d ':'); do \
  cat /proc/irq/$i/smp_affinity_list; done                 # 전부 4
systemctl is-active irqbalance                             # inactive
ps -eo pid,psr,comm | awk '$2==2||$2==3'                   # 연산코어에 vPLC 외 태스크 없어야
```

| 항목 | 기대값 |
|---|---|
| isolated | `2-4` |
| nohz_full | `2-3` |
| max_cstate | `intel_idle`·`processor` 둘 다 `=1` |
| eno1 IRQ affinity | 전부 `4` |
| irqbalance | inactive |
| 연산코어(2,3) 태스크 | vPLC scan만 (다른 거 없음) |

---

## 6. 되돌리기

```bash
sudo systemctl enable --now irqbalance                    # IRQ 핀 원복
# grub 수동 편집 — GRUB_CMDLINE_LINUX서 §1 토큰 제거
#   (isolcpus/nohz_full/rcu_nocbs/irqaffinity/intel_idle.max_cstate/processor.max_cstate)
sudo update-grub && sudo reboot
./scripts/vplc-deploy.sh delete                           # 측정 pod 제거
```

---

## 부록 — tuned 지름길

```bash
sudo apt install tuned
echo 'isolated_cores=2,3,4' | sudo tee /etc/tuned/realtime-variables.conf
sudo tuned-adm profile realtime     # 또는 cpu-partitioning
```
isolcpus+nohz+rcu_nocbs+IRQ를 묶어 적용. 단 eno1 IRQ를 코어4로 보내는 §2는 별도 수행.

---

> **reboot 후 항상 §2(IRQ 핀) 재실행.** grub(격리·cstate)·cilium(§3)은 영구.
> 격리 완료 → vPLC scan cycle + RX eBPF jitter ↓ → 측정이 네트워크 스택 지연만 반영.
> 측정 절차: [`latency-measurement.md`](latency-measurement.md).
