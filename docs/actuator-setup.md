# Actuator(로봇팔) 노드 셋업 런북

> Actuator + Sensor 역할의 노드를 세팅하는 방법

---

## 0. 노드 식별 + 전제

| arm | eno1 IP | RX port | reflect 빌드 플래그 |
|---|---|---|---|
| **arm-1** | 192.168.1.11 | 4840 | (없음 — 기본값) |
| **arm-2** | 192.168.1.12 | 4841 | `-DECHO_SRC_IP=0xC0A8010Cu -DECHO_DPORT=4841` |

```bash
ip -br addr | grep 192.168.1.1     # 내 arm 번호 = .11(arm-1) / .12(arm-2)
ethtool -i eno1 | grep driver      # igb 또는 ixgbe
```
> TSN NIC = `eno1`, 격리 코어 = `3` 가정. 다르면 이하 `eno1`/`3` 치환.

---

## 1. grub — 코어3 격리 + C-state 캡 (reboot 1회)

`/etc/default/grub`의 `GRUB_CMDLINE_LINUX`에 추가 (기존 isolcpus/nohz/rcu/irqaffinity 토큰 있으면 교체):
```
isolcpus=3 rcu_nocbs=3 irqaffinity=0-2,4-11 intel_idle.max_cstate=1 processor.max_cstate=1
```
- `nohz_full` 생략 — NIC softirq 코어라 이득 작음.
- `irqaffinity` = 코어3 뺀 전부(12코어 기준 `0-2,4-11`).

```bash
sudo update-grub && sudo reboot
```

---

## 2. reboot 후 — IRQ 핀 (★ 매 reboot 필요)

```bash
cd ~/dratsn
sudo TSN_IFACE=eno1 NIC_CPU=3 ./scripts/nic-irq-pin.sh
```
> ★ 변수는 **`sudo` 뒤**에. 앞에 두면(`VAR=x sudo …`) sudo가 버려 기본값(코어4)으로 핀됨.

---

## 3. cilium XDP 가속 끄기 (클러스터 1회, 영구)

cilium이 eno1 XDP(`cil_xdp_entry`) 점유하면 reflect 못 붙음. 끄기:
```bash
kubectl -n kube-system edit cm cilium-config     # bpf-lb-acceleration: disabled
kubectl -n kube-system rollout restart ds/cilium
```

---

## 4. reflect 빌드 + attach (★ attach는 매 reboot 필요)

```bash
cd ~/DRA-TSN/examples/mock-actuator
make                                              # arm-1
# arm-2: make CFLAGS_EXTRA='-DECHO_SRC_IP=0xC0A8010Cu -DECHO_DPORT=4841'
IFACE=eno1 sudo ./load.sh attach                  # rxvlan off 자동 처리
```
> clang 없으면: `sudo apt install -y clang llvm libbpf-dev`

---

## 5. 검증 (전부 통과해야 완료)

```bash
cat /sys/devices/system/cpu/isolated                       # → 3
grep -o 'max_cstate=[0-9]' /proc/cmdline                   # intel_idle/processor 둘 다 =1
grep . /sys/devices/system/cpu/cpu3/cpuidle/state*/name    # → POLL, C1 만 (C1E/C3/C6 사라짐 = 캡 성공)
for i in $(grep eno1 /proc/interrupts|awk '{print $1}'|tr -d ':'); do \
  cat /proc/irq/$i/smp_affinity_list; done                 # 전부 3
systemctl is-active irqbalance                             # inactive
ethtool -k eno1 | grep rx-vlan-offload                     # off
sudo bpftool prog show id $(ip -d link show eno1|grep -oP 'prog/xdp id \K[0-9]+') | grep -o 'name [^ ]*'
#   → name tsn_reflect   (cil_xdp_entry 면 cilium 점유 = §3 재확인)
```

| 항목 | 기대값 |
|---|---|
| isolated | `3` |
| cpu3 cstate | `POLL`+`C1`만 존재 (깊은 거 사라짐) |
| eno1 IRQ affinity | 전부 `3` |
| irqbalance | inactive |
| rx-vlan-offload | off |
| eno1 xdp prog | `tsn_reflect` |

### 왕복 실측 (w1서 probe 송신 중)
```bash
sudo tcpdump -ni eno1 'vlan and (udp port 4840 or udp port 4841)' -c 10   # 인/아웃 둘 다
```

---

## 6. 되돌리기

```bash
# 런타임(reboot 전 원복)
cd ~/DRA-TSN/examples/mock-actuator && IFACE=eno1 sudo ./load.sh detach
sudo ethtool -K eno1 rxvlan on
sudo systemctl enable --now irqbalance
# grub 수동 편집 — GRUB_CMDLINE_LINUX서 §1 토큰 제거
#   (isolcpus/rcu_nocbs/irqaffinity/intel_idle.max_cstate/processor.max_cstate)
sudo update-grub && sudo reboot
```

---

> ★ **reboot 후 항상 §2(IRQ 핀) + §4(attach) 재실행.** grub(격리·cstate)·cilium(§3)은 영구.
> 자동화하려면 §2+§4를 systemd oneshot 유닛(`After=network-online.target`)으로.
