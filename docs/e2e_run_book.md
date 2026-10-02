# 데이터플레인 수동 검증 런북 (tcpdump E2E)

> TX(VLAN 태깅/SNAT) + RX(DNAT/VLAN strip → pod 도착)를 tcpdump로 눈으로 확인. `dataplane_e2e.sh` 자동화의 수동판.

> **캡처 지점이 TX/RX 다름**: TX는 host(veth·eno1)서 보임. RX는 XDP redirect가 normal 탭 우회 → **pod 쪽에서만** 보임.

---

## 0. 전제 + 변수 조회

- `mock-vplc` Running on alpha-w1, TSN claim mount. 배포: `./tests/loop.sh scenario` → `kubectl apply -f deployments/examples/mock-vplc.yaml`.
- CNC 스트림 4개 존재 (arm-1: s-tx/s-rx 192.168.1.11, arm-2: s-tx2/s-rx2 192.168.1.12. TX vlan200/pcp6, RX vlan100/pcp5).
- w1 호스트 root + m1 kubectl. 이하 예시 = **arm-1**.

```bash
# (m1) pod IP + host-veth ifindex
PODIP=$(kubectl -n dratsn-test get pod mock-vplc -o jsonpath='{.status.podIP}'); echo $PODIP
IFX=$(kubectl -n dratsn-test exec mock-vplc -- cat /sys/class/net/eth0/iflink); echo $IFX
# (w1 호스트) ifindex → veth 이름 + NIC MAC
VETH=$(ip -o link | awk -F': ' -v i=$IFX '$1==i{print $2}' | cut -d@ -f1); echo $VETH
NICMAC=$(cat /sys/class/net/eno1/address); echo $NICMAC
```

---

## 1. TX — VLAN 태깅 + L2 재작성 (3지점 캡처)

터미널 4개 (캡처 3 + 트리거 1).

```bash
# T1 (w1) [veth] 원본 — VLAN 없음, src=pod
sudo tcpdump -i $VETH -e -nn 'udp and dst host 192.168.1.11'
# T2 (w1) [eno1] 변환 후 — vlan200, src=eno1 MAC, dst=0c:c4:7a:c4:a7:2a
sudo tcpdump -i eno1 -e -nn 'vlan 200 and dst host 192.168.1.11'
# T3 (arm-1 호스트) [eno1] wire 도착
sudo tcpdump -i eno1 -e -nn 'vlan 200 and dst host 192.168.1.11'
# T4 (m1) 송신 트리거 (10회 자동종료)
kubectl -n dratsn-test exec mock-vplc -- sh -c 'for i in $(seq 10); do echo -n X | nc -u -w1 192.168.1.11 4840; done'
```

기대:
- **T1** = 태그 없음, `$PODIP.xxxx > 192.168.1.11.4840`.
- **T2** = `vlan 200, p 6`, src/dst MAC 재작성 (TX 변환).
- **T3** = T2 프레임이 wire 건너 도착. 안 뜨면 → 스위치 trunk vlan200 미전달 or arm-1 `eno1.200` subif 미생성(`cat /proc/net/vlan/config`, ADR-0025).

> ⚠️ eno1 필터에 `vlan 200` 키워드 필수 — 없으면 802.1Q 태그(offset12=0x8100) 때문에 `dst host` 단독은 0건. 교차확인: `cat /sys/class/net/eno1/statistics/tx_packets` 델타.

---

## 2. SNAT — src IP 토글

```bash
kubectl -n kube-system set env ds/dratsn TSN_TX_SNAT=true
kubectl -n kube-system rollout status ds/dratsn --timeout=120s
```
T2 재캡처 → src IP `$PODIP` → `192.168.1.1`(w1 eno1 IP, per-node) = SNAT 확인.
끄기: `kubectl -n kube-system set env ds/dratsn TSN_TX_SNAT-` 후 rollout. (고정 IP: `TSN_SNAT_SOURCE_IP=<ip>`)

---

## 3. RX — DNAT + VLAN strip → pod 도착

> RX는 XDP redirect가 탭 우회 → **eno1·veth엔 안 뜸. pod 쪽에서만 확인.**

pod 확인 (inject 前에 먼저 띄움, 택1):
```bash
# (A) nc 수신 — 소켓 수신 = strip+DNAT+redirect 전부 OK
kubectl -n dratsn-test exec mock-vplc -- timeout 20 nc -u -l -p 4840
# (B) eth0 직접 캡처 — 도착까지 (csum 무관)
kubectl debug -it -n dratsn-test mock-vplc --image=nicolaka/netshoot -- tcpdump -i eth0 -nn udp
```

로봇팔 노드(arm-1)서 주입:
```bash
W1MAC=$(kubectl get tsnnic alpha-w1-eno1 -o jsonpath='{.spec.macAddress}'); echo $W1MAC   # (m1)
# (arm-1 호스트, root) vlan100 raw 주입
sudo PEER_IFACE=eno1 TSN_NIC_MAC=$W1MAC RX_ENDPOINT_IP=192.168.1.1 RX_VLAN=100 \
  RX_TALKER_IP=192.168.1.11 ./dataplane_e2e.sh inject-rx
```

기대:
- pod **(A)** = `DRATSN-RX-WIRE` x10. pod **(B)** = `192.168.1.11.xxxx > $PODIP.4840`, VLAN 없음(strip) + dst=pod IP(DNAT).
- eno1·veth = 안 뜸 = 정상.

> `RX_TALKER_IP`(s-rx talker)가 **rx_flows key**. src IP≠이값 or vlan≠`RX_VLAN`이면 XDP_PASS → pod 미도달. arm-2는 `RX_TALKER_IP=192.168.1.12`(포트 4841).

pod에 안 뜨면 (순서대로):
1. `sudo RX_VLAN=100 ./tests/dataplane_e2e.sh rx` — 로직만(BPF_PROG_TEST_RUN, wire 불요).
2. `sudo bpftool map dump pinned /sys/fs/bpf/tsn/rx_flows` — key=talker→endpoint:port, val=현 pod IP+vid100 맞나.
3. 스위치 trunk vlan100 운반 + `cat /sys/class/net/eno1/statistics/rx_packets` 델타(XDP가 먹어 tcpdump 안 뜨므로 카운터로 확인).

---

## 트러블슈팅

- **eno1 캡처 0**: `kubectl debug node` tcpdump는 불안정 → **w1 호스트서 직접** `sudo tcpdump`. egress는 `tx_packets` 델타로 확인.
- **`dst host` 단독 0건(veth는 잡힘)**: eno1 프레임은 wire 802.1Q 태그(0x8100) → `vlan 200 and ...` 또는 `ether dst <mac>` 로 필터.
- **cilium drop**: `kubectl -n kube-system exec <cilium-pod-w1> -- cilium-dbg monitor --type drop` — TSN 플로우 drop 0이어야.
</content>
